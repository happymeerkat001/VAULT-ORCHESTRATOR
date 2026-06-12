#!/usr/bin/env python3
"""
hermes_worker.py — Polling worker for the ## Hermes-to-do 🪶 section.

Reads today's daily note, picks the first unchecked item that isn't already
in progress, runs a constrained MiniMax tool-use loop to break it down and
do the work, writes the result to ~/.../AI-Vault/Hermes Output/, marks the
item done in the daily note, and (optionally) pushes it to GitHub Issues
via cli/hermes_to_kanban.py.

Tools exposed to the model (all path-sandboxed to the vault root):
    read_file, list_directory, search_files,
    web_fetch, web_search,
    make_directory, move_file, write_output_file

Stdlib only. No build, no tests, no pip deps. Reads GITHUB_TOKEN, MINIMAX_API_KEY
from .env (repo root).

Web tools (web_fetch, web_search) are READ-ONLY. They perform outbound HTTP via
urllib with a 30s per-call timeout and a hard response-size cap; they never
write to the vault and never read non-public/internal addresses (localhost,
RFC1918 ranges, file://, ftp://, etc. are rejected).

Vault write policy: by user request, the worker may write anywhere inside the
vault, not just Hermes Output/ and z.Ingestion/. The only special case is the
daily note's Hermes-to-do section, which remains owned by the worker runtime
itself (the LLM must not edit that section directly).
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
VAULT_ROOT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault"
).expanduser()
DAILY_NOTES_PATH = VAULT_ROOT / "Daily Notes"
OUTPUT_DIR = VAULT_ROOT / "Hermes Output"
HERMES_HEADER = "## Hermes-to-do 🪶"

MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"
MAX_LOOP_ITERATIONS = 20
MAX_WALL_SECONDS = 180
# How long a single web_fetch / web_search call may run before we kill it.
WEB_TOOL_TIMEOUT = 30
# Hard cap on response size from a single web_fetch (chars). Keeps one page
# from blowing out the LLM context window.
WEB_FETCH_MAX_CHARS = 40_000
# DuckDuckGo HTML endpoint used for web_search (no API key required).
WEB_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"

# ----------------------------- env loading ---------------------------------

def load_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _ensure_ssl_works() -> None:
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi  # type: ignore

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


_ensure_ssl_works()


# --------------------------- path sandboxing --------------------------------

def safe_path(raw: str) -> Path:
    """Resolve `raw` to an absolute path and assert it lives under VAULT_ROOT.

    Rejects '..', absolute paths outside the vault, and symlinks pointing out.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (VAULT_ROOT / p).resolve()
    else:
        p = p.resolve()
    vault = VAULT_ROOT.resolve()
    try:
        p.relative_to(vault)
    except ValueError as exc:
        raise ValueError("path escapes vault root: %s" % raw) from exc
    return p


def safe_writable_path(raw: str) -> Path:
    """Resolve a writable path inside the vault.

    By user request, the worker may write anywhere inside VAULT_ROOT. The only
    sandbox boundary is the vault root itself; paths outside the vault are
    rejected by safe_path().
    """
    return safe_path(raw)


# ----------------------------- tools ----------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the UTF-8 text of a file inside the vault. Returns up to N lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the vault, or absolute path inside the vault."},
                    "max_lines": {"type": "integer", "description": "Cap on lines returned (default 200)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List immediate entries of a directory inside the vault. Returns names + a flag marking directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path inside the vault."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Recursively find files under a directory whose name matches a glob pattern (case-insensitive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Directory to search."},
                    "glob": {"type": "string", "description": "Filename pattern, e.g. '*Hermes*' or '*.md'."},
                },
                "required": ["root", "glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a public URL and return its content as plain text. "
                "HTML is stripped to text; very long pages are truncated to "
                "WEB_FETCH_MAX_CHARS chars. Use this to read docs, blog posts, "
                "API references, or any other web resource needed to complete "
                "the task. Read-only; cannot be used to write anywhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web via DuckDuckGo HTML and return the top "
                "result titles, snippets, and URLs. No API key required. Use "
                "this when you need to discover URLs or compare options. "
                "Read-only; cannot be used to write anywhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'MiniMax M3 release benchmarks'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on number of results returned (default 8, max 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_directory",
            "description": "Create a directory (with parents) inside the vault. Idempotent.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file/directory inside the vault. Creates parent dirs of the destination if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dst": {"type": "string"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_output_file",
            "description": "Write text to a file anywhere inside the vault (overwrites if exists). Use this for final deliverables in Hermes Output/ and for direct note updates when the task asks for it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _read_text_with_retry(path: Path, attempts: int = 5) -> str:
    last = None
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            time.sleep(0.4 * (2 ** i))
    raise last  # type: ignore[misc]


def _write_text_with_retry(path: Path, content: str, attempts: int = 5) -> None:
    last = None
    for i in range(attempts):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last = exc
            time.sleep(0.4 * (2 ** i))
    raise last  # type: ignore[misc]


# iCloud aggressively holds file locks on the daily note (Obsidian itself is
# constantly reading/writing it). On contended ticks the default 5-attempt
# backoff (0.4 + 0.8 + 1.6 + 3.2 + 6.4 = ~12s) is not enough and the tick
# logs `ERROR: read daily note failed: [Errno 11] Resource deadlock avoided`
# every 30s, producing a long string of useless tick cycles. Use a much longer
# retry on daily-note reads specifically: 10 attempts, 1s base, capped at 32s
# per backoff, with a small random jitter so two concurrent LaunchAgent ticks
# don't synchronize their retries and starve iCloud.
DAILY_NOTE_READ_ATTEMPTS = 10
DAILY_NOTE_READ_BASE_DELAY = 1.0
DAILY_NOTE_READ_MAX_DELAY = 32.0
DAILY_NOTE_WRITE_ATTEMPTS = 10


def _read_daily_note_with_retry(path: Path) -> str:
    """Read the daily note with a long, jittered retry budget for EDEADLK."""
    last: Exception | None = None
    for i in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            # Last attempt: no sleep, raise immediately on the way out.
            if i == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** i), DAILY_NOTE_READ_MAX_DELAY)
            # Jitter ±20% so two simultaneous ticks don't retry in lockstep.
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None  # for type-checker
    raise last


def _write_daily_note_with_retry(path: Path, content: str) -> None:
    """Write the daily note with a long, jittered retry budget for EDEADLK.

    Mirror of `_read_daily_note_with_retry`. The 10-attempt budget is generous
    on purpose: under sustained iCloud contention this is the only retry loop
    standing between the worker and an exit-1 tick, so we accept a longer
    wait in exchange for not wasting a tick cycle on a recoverable error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for i in range(DAILY_NOTE_WRITE_ATTEMPTS):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last = exc
            if i == DAILY_NOTE_WRITE_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** i), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def tool_read_file(args: dict) -> str:
    p = safe_path(args["path"])
    if not p.exists():
        return "(file does not exist)"
    if p.is_dir():
        return "(path is a directory; use list_directory)"
    text = _read_text_with_retry(p)
    limit = int(args.get("max_lines") or 200)
    lines = text.splitlines()
    if len(lines) > limit:
        return "\n".join(lines[:limit]) + "\n…(truncated at %d lines)" % limit
    return text


def tool_list_directory(args: dict) -> str:
    p = safe_path(args["path"])
    if not p.exists():
        return "(directory does not exist)"
    if not p.is_dir():
        return "(not a directory)"
    entries = []
    for child in sorted(p.iterdir()):
        suffix = "/" if child.is_dir() else ""
        entries.append(child.name + suffix)
    return "\n".join(entries) if entries else "(empty)"


def tool_search_files(args: dict) -> str:
    root = safe_path(args["root"])
    if not root.exists() or not root.is_dir():
        return "(root does not exist or is not a directory)"
    pattern = args["glob"].lower()
    matches: list[str] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        if pattern.replace("*", "").lower() in candidate.name.lower():
            try:
                matches.append(str(candidate.relative_to(VAULT_ROOT)))
            except ValueError:
                continue
    return "\n".join(matches[:200]) if matches else "(no matches)"


# ---- Web tools (read-only) -----------------------------------------------
# These never touch the vault filesystem. They are sandboxed to outbound HTTP
# only; the LLM cannot use them to write anywhere. Network calls are bounded
# by WEB_TOOL_TIMEOUT and a hard response-size cap (WEB_FETCH_MAX_CHARS) so a
# runaway page cannot blow out the LLM context window.

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ALLOWED_SCHEMES = ("http://", "https://")


def _normalize_url(url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url.startswith(_ALLOWED_SCHEMES):
        return None
    # Block obvious SSRF targets: localhost, link-local, RFC1918 ranges,
    # IPv6 loopback, and non-http(s) schemes that browsers won't follow.
    lowered = url.lower()
    blocked_substrings = (
        "localhost", "127.", "0.0.0.0", "169.254.", "10.",
        "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
        "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
        "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
        "::1", "fc", "fd", "fe80:",
        "file:", "ftp:", "gopher:", "dict:",
    )
    for bad in blocked_substrings:
        if bad in lowered:
            return None
    return url


def _strip_html_to_text(html: str) -> str:
    """Best-effort HTML → plain text. Stdlib only."""
    # Remove script/style blocks first (their content is not visible text).
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Convert common block tags to newlines so the text stays readable.
    html = re.sub(r"<(?:br|/p|/div|/li|/h[1-6])\b[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Drop all remaining tags.
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode the most common HTML entities.
    html = (
        html.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse whitespace per line, then trim blank lines.
    out_lines = []
    for raw_line in html.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            out_lines.append(line)
    return "\n".join(out_lines)


def tool_web_fetch(args: dict) -> str:
    url = _normalize_url(args.get("url", ""))
    if not url:
        return "error: url must be an absolute http(s) URL pointing to the public web"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=WEB_TOOL_TIMEOUT) as resp:
            raw = resp.read(WEB_FETCH_MAX_CHARS * 4, )  # generous on raw; we truncate after strip
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as exc:
        return "fetch error: %s" % exc
    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    text = _strip_html_to_text(html)
    if len(text) > WEB_FETCH_MAX_CHARS:
        text = text[:WEB_FETCH_MAX_CHARS] + "\n…(truncated at %d chars)" % WEB_FETCH_MAX_CHARS
    return "URL: %s\nStatus: fetched\n\n%s" % (url, text)


def tool_web_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "error: query is required"
    try:
        max_results = int(args.get("max_results") or 8)
    except (TypeError, ValueError):
        max_results = 8
    max_results = max(1, min(20, max_results))
    try:
        form = urllib.parse.urlencode({"q": query}).encode("ascii")
        req = urllib.request.Request(
            WEB_SEARCH_ENDPOINT,
            data=form,
            method="POST",
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=WEB_TOOL_TIMEOUT) as resp:
            html = resp.read(WEB_FETCH_MAX_CHARS * 4).decode("utf-8", errors="replace")
    except Exception as exc:
        return "search error: %s" % exc
    # Parse the DDG HTML result list. The result__a link and the
    # result__snippet anchor are siblings inside the same result block but are
    # not adjacent (the result__icon, result__url, etc. sit between them). We
    # collect both, then pair them by order.
    title_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    snippet_re = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    titles = title_re.findall(html)
    snippets = snippet_re.findall(html)
    if not titles:
        return "(no results)"

    def _clean(snippet_html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", snippet_html)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        return re.sub(r"\s+", " ", text).strip()

    out: list[str] = []
    for i, (href, title_html) in enumerate(titles[:max_results]):
        title = _clean(title_html)
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        # DDG result URLs go through a redirector; try to lift the real target.
        real = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            try:
                real = urllib.parse.unquote(m.group(1))
            except Exception:
                pass
        out.append("- %s\n  %s\n  %s" % (title, snippet, real))
    return "Query: %s\nResults: %d\n\n%s" % (query, len(out), "\n\n".join(out))


def tool_make_directory(args: dict) -> str:
    p = safe_writable_path(args["path"])
    p.mkdir(parents=True, exist_ok=True)
    return "ok: " + str(p.relative_to(VAULT_ROOT))


def tool_move_file(args: dict) -> str:
    src = safe_path(args["src"])
    dst = safe_writable_path(args["dst"])
    if not src.exists():
        return "(source does not exist)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return "moved: %s -> %s" % (src.relative_to(VAULT_ROOT), dst.relative_to(VAULT_ROOT))


def tool_write_output_file(args: dict) -> str:
    p = safe_writable_path(args["path"])
    _write_text_with_retry(p, args["content"])
    return "wrote: " + str(p.relative_to(VAULT_ROOT)) + " (%d bytes)" % len(args["content"])


TOOL_DISPATCH = {
    "read_file": tool_read_file,
    "list_directory": tool_list_directory,
    "search_files": tool_search_files,
    "web_fetch": tool_web_fetch,
    "web_search": tool_web_search,
    "make_directory": tool_make_directory,
    "move_file": tool_move_file,
    "write_output_file": tool_write_output_file,
}


# ----------------------- daily-note section parsing -------------------------

def find_today_note(today: str | None) -> Path:
    date_str = today or datetime.now().strftime("%Y-%m-%d")
    return DAILY_NOTES_PATH / (date_str + ".md")


def extract_hermes_section(note_text: str) -> tuple[int, int, list[tuple[int, str, str]]]:
    """Return (start_line, end_line, items) for the Hermes section.

    items: list of (line_index, status, text_without_checkbox) where status is
    one of 'open', 'in_progress', 'done'.
    """
    lines = note_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == HERMES_HEADER:
            start = i
            break
    if start is None:
        return -1, -1, []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            end = j
            break
    items: list[tuple[int, str, str]] = []
    for k in range(start + 1, end):
        raw = lines[k]
        m_done = re.match(r"^- \[x\]\s+(.+)$", raw.strip(), re.IGNORECASE)
        m_prog = re.match(r"^- \[~\]\s+(.+)$", raw.strip())
        m_fail = re.match(r"^- \[!\]\s+(.+)$", raw.strip())
        m_open = re.match(r"^- \[ \]\s+(.+)$", raw.strip())
        if m_done:
            items.append((k, "done", m_done.group(1)))
        elif m_prog:
            items.append((k, "in_progress", m_prog.group(1)))
        elif m_fail:
            items.append((k, "failed", m_fail.group(1)))
        elif m_open:
            items.append((k, "open", m_open.group(1)))
    return start, end, items


def next_open_item(note_text: str) -> tuple[int, str] | None:
    """Return (line_index, raw_task_text) of the next item to work on.

    Prefers in-progress (`- [~]`) items first (crash recovery), then open
    (`- [ ]`) items. Skips `done` (`- [x]`) and `failed` (`- [!]`) items.
    Returns None if the section is empty or all done.

    Failed items are NEVER picked up automatically. They are visible in the
    daily note for human review; rerun them by editing `- [!]` back to `- [ ]`.
    """
    _, _, items = extract_hermes_section(note_text)
    for line_idx, status, text in items:
        if status == "in_progress":
            return line_idx, _strip_running_suffix(text)
    for line_idx, status, text in items:
        if status == "open":
            return line_idx, text
    return None


def _strip_running_suffix(text: str) -> str:
    """Remove one or more '_(running)_' suffixes left by mark_in_progress on prior runs."""
    return re.sub(r"(\s*_\(running\)_)+\s*$", "", text).strip()


# Recognised effort hints at the start of a task line:
#   [1800s]    -> 1800 second wall clock budget for this task
#   [60i]      -> 60 LLM iterations for this task
#   [1800s,60i] -> both at once
# Examples:
#   - [ ] [1800s] research M3, M2.7, Claude Fable 5 and add to Ai Comparison Table
#   - [ ] [600s,40i] compare OpenRouter pricing
_EFFORT_HINT_RE = re.compile(
    r"^\s*\[(?P<flags>[^\]]+)\]\s*"
)


def parse_effort_hint(task_text: str) -> tuple[str, int, int]:
    """Extract optional [NNNs] and/or [NNNi] flags from the start of a task.

    Returns (cleaned_text, max_seconds, max_iterations). A 0 in either
    override field means "use the module default" (i.e. the worker should
    fall back to MAX_WALL_SECONDS / MAX_LOOP_ITERATIONS for that task).
    Unknown tokens inside the brackets are ignored and the bracket is left
    in place for the LLM to read.
    """
    max_seconds = 0
    max_iters = 0
    cleaned = task_text
    m = _EFFORT_HINT_RE.match(cleaned)
    if not m:
        return cleaned, max_seconds, max_iters
    raw_flags = m.group("flags")
    seconds_match = re.search(r"(\d+)\s*s\b", raw_flags)
    if seconds_match:
        max_seconds = int(seconds_match.group(1))
    iters_match = re.search(r"(\d+)\s*i\b", raw_flags)
    if iters_match:
        max_iters = int(iters_match.group(1))
    if max_seconds > 0 or max_iters > 0:
        cleaned = cleaned[m.end():].strip()
    else:
        # No recognised token — leave the text alone, including the bracket.
        return task_text, 0, 0
    return cleaned, max_seconds, max_iters


_ABS_MD_PATH_RE = re.compile(r"(/[^\n\r]+?\.md)")


def extract_absolute_md_paths(task_text: str) -> list[str]:
    """Return absolute .md paths mentioned in the task, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _ABS_MD_PATH_RE.findall(task_text):
        candidate = raw.rstrip(")],.;:'\"“”")
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def infer_target_note_path(task_text: str) -> str:
    """Best-effort guess at an explicit target markdown note path in the task.

    Preference signals:
    - nearby verbs like update/edit/write/append/add/merge
    - nearby phrases like target note/note/chart/table
    Negative signals:
    - nearby source-ish words like using/from/source/transcript
    - paths under Hermes Output/ (deliverables are usually not the target note)
    """
    paths = extract_absolute_md_paths(task_text)
    best_path = ""
    best_score = 0
    lower = task_text.lower()
    for path in paths:
        idx = lower.find(path.lower())
        if idx < 0:
            continue
        window = lower[max(0, idx - 80): min(len(lower), idx + len(path) + 80)]
        score = 0
        if any(token in window for token in ("target note", "target file", "target md")):
            score += 5
        if any(token in window for token in ("update", "edit", "write to", "append to", "add to", "merge into", "patch")):
            score += 4
        if any(token in window for token in (" note", " chart", " table", " comparison table", " file")):
            score += 2
        if any(token in window for token in ("using", "from", "source", "transcript", "based on", "read this")):
            score -= 4
        if "transcript" in path.lower():
            score -= 5
        if "/hermes output/" in path.lower():
            score -= 3
        if score > best_score:
            best_score = score
            best_path = path
    return best_path if best_score > 0 else ""


# ----------------------- daily-note mutation --------------------------------

def mark_in_progress(note_path: Path, line_idx: int, task_text: str) -> None:
    """Rewrite a `- [ ]` line as `- [~] ... _(running)_` and write back.

    The daily note is the single most-contended iCloud file in the vault, so
    both the read-modify-write round-trip and the write itself need a long,
    jittered retry budget. EDEADLK here is recoverable; the previous 5x/0.4s
    budget wedged the worker for minutes under load.
    """
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            if line_idx < len(lines):
                lines[line_idx] = "- [~] " + task_text + "  _(running)_"
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def _format_done_suffix(relpath: str) -> str:
    """Render the "see deliverable" marker with an Obsidian wikilink.

    A bare relative path is converted into a wikilink using the filename stem
    so it renders as an active link in Obsidian. Stems are unique enough within
    a single day's Hermes Output/ set to disambiguate without the date prefix
    leaking into the link text. If the relpath is empty or the link cannot be
    derived, fall back to a plain string in parentheses.
    """
    if not relpath:
        return "  _(no output file written)_"
    if not relpath.lower().endswith(".md"):
        return "  _(→ see " + relpath + ")_"
    name = relpath.rsplit("/", 1)[-1]
    stem = name[:-3] if name.lower().endswith(".md") else name
    return "  _(→ see [[%s]])_" % stem


def mark_done(
    note_path: Path,
    line_idx: int,
    original_text: str,
    output_relpath: str,
) -> None:
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            if line_idx < len(lines):
                lines[line_idx] = (
                    "- [x] " + original_text + _format_done_suffix(output_relpath)
                )
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def mark_open(note_path: Path, line_idx: int, original_text: str) -> None:
    """Restore a task to unchecked/open state after a failed run."""
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            if line_idx < len(lines):
                lines[line_idx] = "- [ ] " + original_text
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


# ----------------------- LLM tool-use loop ---------------------------------

SYSTEM_PROMPT = (
    "You are Hermes, an autonomous task worker. The user added an unchecked "
    "task to today's daily note under '## Hermes-to-do 🪶'. Your job is to "
    "do that task end-to-end using the tools provided.\n\n"
    "Rules:\n"
    "1. A pre-task breakdown is created before you begin execution. Follow it "
    "as a working checklist, but adapt if tool results show a better path.\n"
    "2. Use tools to gather context, perform actions, and verify the result.\n"
    "3. You may READ and WRITE any file inside the vault when needed to "
    "complete the task. The only exception is the Hermes-to-do section of the "
    "daily note: the worker script manages that section state on your behalf.\n"
    "4. When the task is research, analysis, or synthesis, prefer writing a "
    "final markdown deliverable to 'Hermes Output/' using write_output_file. "
    "If the task explicitly asks you to update an existing note, you may write "
    "that note directly instead.\n"
    "5. After writing the deliverable or updating the target note, your final "
    "assistant message should be plain text: a 2-4 sentence summary of what you "
    "did, with the path(s) you wrote. Do not call any more tools after writing.\n"
    "6. NEVER add new items to the Hermes-to-do section. NEVER edit the "
    "daily note's Hermes-to-do checkbox state yourself. The worker script will "
    "mark the task done or restore it to open.\n"
    "7. If the task cannot be done with the available tools, do not pretend it "
    "succeeded. Return a brief explanation of what was attempted and why it "
    "failed. The worker script will leave the checkbox unchecked so it can be "
    "retried later.\n"
    "8. The daily note uses task states like `- [ ]` open, `- [~]` in progress "
    "(with `_(running)_` suffix), and `- [x]` done. You may see `_(running)_` "
    "markers while reading the vault; treat them as worker bookkeeping, not as "
    "content to edit directly.\n"
    "9. For research tasks that need up-to-date public information, use "
    "web_search to discover sources, then web_fetch to read specific pages. "
    "web_search is the DuckDuckGo HTML endpoint (no API key); web_fetch is "
    "a plain HTTP GET that strips HTML to text. Both are READ-ONLY — they "
    "cannot write to the vault or to any external service. Each call has a "
    "30s timeout and a per-page size cap. Cite the URLs you actually read in "
    "the deliverable so the user can verify.\n"
    "10. The user's task line may begin with an effort hint in square "
    "brackets, e.g. `[1800s]` to extend the per-task wall-clock budget for "
    "this single tick. If you see one, treat the task as high-effort and "
    "plan for up to that many seconds of total work; do not pad with idle "
    "loops to burn the budget."
)


def call_minimax(
    messages: list[dict],
    api_key: str,
    tools: list[dict] | None = TOOLS,
    tool_choice: str | None = "auto",
) -> dict:
    body_obj: dict = {
        "model": "MiniMax-M2.7",
        "messages": messages,
        "temperature": 0.2,
    }
    if tools is not None:
        body_obj["tools"] = tools
    if tool_choice is not None:
        body_obj["tool_choice"] = tool_choice
    body = json.dumps(body_obj).encode("utf-8")
    auth_value = "Bearer" + " " + api_key
    req = urllib.request.Request(
        MINIMAX_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_value,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("MiniMax HTTP %s: %s" % (exc.code, detail[:400])) from exc


def build_task_breakdown(
    task_text: str,
    today: str,
    max_seconds: int,
    max_iterations: int,
    api_key: str,
    explicit_md_paths: list[str],
    target_note_path: str,
) -> str:
    """Create a small execution checklist before starting work.

    This intentionally runs before the tool-use loop so large daily-note tasks
    are decomposed into manageable parts. It does not get tools and must not
    make claims about the vault contents; it is only an execution plan.
    """
    target_line = target_note_path or "(none detected)"
    paths_line = "\n".join("- " + p for p in explicit_md_paths) if explicit_md_paths else "(none)"
    messages = [
        {
            "role": "system",
            "content": (
                "You decompose one Hermes-to-do task into a practical execution "
                "checklist before any work begins. Return only a compact numbered "
                "list with 3-7 manageable parts. Do not use tools. Do not claim "
                "you have read files or completed anything. Include verification "
                "as the final part."
            ),
        },
        {
            "role": "user",
            "content": (
                "Today's date: %s\n"
                "Task: %s\n"
                "Per-task budget: %d seconds, %d LLM iterations.\n"
                "Explicit target note path: %s\n"
                "Markdown paths mentioned:\n%s\n\n"
                "Break this into smaller, manageable parts before execution."
            ) % (today, task_text, max_seconds, max_iterations, target_line, paths_line),
        },
    ]
    try:
        data = call_minimax(messages, api_key, tools=None, tool_choice=None)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
        if content:
            return content[:3000]
    except Exception:
        pass

    fallback = [
        "1. Identify the target deliverable and any explicitly named source or target notes.",
        "2. Read the relevant local notes and/or fetch the minimum needed external sources.",
        "3. Extract the key facts, decisions, quotes, and gaps relevant to the task.",
        "4. Update the target note directly, or write a focused Hermes Output deliverable if no target note is clear.",
        "5. Verify the written result answers the original task and points to the correct output path.",
    ]
    return "\n".join(fallback)


def run_task(
    task_text: str,
    today: str,
    api_key: str,
    max_seconds: int = 0,
    max_iterations: int = 0,
) -> tuple[bool, str, str]:
    """Run a single task to completion. Returns (ok, summary, output_path).

    Per-task limits default to MAX_LOOP_ITERATIONS / MAX_WALL_SECONDS when the
    caller passes 0 (sentinel for "use default"). Callers can pass overrides
    (typically read from the [NNNs]/[NNNi] effort hint at the start of the
    task line). Overrides are clamped to safe bounds so a typo in the daily
    note cannot lock up the worker indefinitely.
    """
    # Resolve 0-sentinels to the module defaults, then clamp both to safe bounds.
    eff_seconds = MAX_WALL_SECONDS if max_seconds <= 0 else max_seconds
    eff_iters = MAX_LOOP_ITERATIONS if max_iterations <= 0 else max_iterations
    eff_seconds = max(30, min(eff_seconds, 3600))      # 30s .. 1h
    eff_iters = max(2, min(eff_iters, 200))            # 2 .. 200 iterations
    # If the user only set a wall-clock budget (e.g. `- [ ] [1200s] research M3`),
    # derive a reasonable iteration ceiling from it so the seconds hint actually
    # buys more tool-use steps. Empirically, research tasks average 3-6s/iter
    # (read_file + LLM round-trip + tool dispatch). 4s/iter is a conservative
    # floor; clamp at MAX_LOOP_ITERATIONS=20 on the low end and the 200-iter
    # hard cap on the high end.
    if max_seconds > 0 and max_iterations <= 0:
        eff_iters = max(MAX_LOOP_ITERATIONS, min(200, max_seconds // 4))
    max_seconds = eff_seconds
    max_iterations = eff_iters

    explicit_md_paths = extract_absolute_md_paths(task_text)
    target_note_path = infer_target_note_path(task_text)
    task_breakdown = build_task_breakdown(
        task_text=task_text,
        today=today,
        max_seconds=max_seconds,
        max_iterations=max_iterations,
        api_key=api_key,
        explicit_md_paths=explicit_md_paths,
        target_note_path=target_note_path,
    )

    user_msg = (
        "Today's date: %s\n"
        "Vault root: %s\n"
        "Task: %s\n"
        "Per-task budget: %d seconds wall-clock, %d LLM iterations.\n\n"
        "Pre-task breakdown created before execution:\n%s\n\n"
        "Begin. Work through the breakdown in small parts. Use tools to do "
        "the work and verify each major step. Prefer updating an explicit target "
        "note path directly when the task provides one. Otherwise, for research "
        "or synthesis tasks, write your deliverable to Hermes Output/%s <safe-name>.md, "
        "then reply with a 2-4 sentence summary."
    ) % (today, VAULT_ROOT, task_text, max_seconds, max_iterations, task_breakdown, today)

    if target_note_path:
        user_msg += (
            "\n\nExplicit target note path detected: %s\n"
            "Primary completion criterion: update that note directly. You may also "
            "write a Hermes Output deliverable if it helps, but do not stop after "
            "only writing a deliverable when the task clearly names a target note."
        ) % target_note_path
    elif explicit_md_paths:
        user_msg += (
            "\n\nExplicit markdown paths mentioned in the task:\n- %s\n"
            "If one of these is clearly the destination note, prefer editing it "
            "directly; otherwise treat them as source files and use Hermes Output/ "
            "for the final deliverable."
        ) % "\n- ".join(explicit_md_paths)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    start = time.time()
    written_paths: list[str] = []
    for iteration in range(max_iterations):
        if time.time() - start > max_seconds:
            return False, "timed out after %ds" % max_seconds, ""

        try:
            data = call_minimax(messages, api_key)
        except Exception as exc:
            return False, "LLM call failed: %s" % exc, ""

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        # Strip leaked reasoning blocks
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)

        if not tool_calls:
            # Final answer. Prefer the most recent path written during this run;
            # if nothing was written, fall back to the latest Hermes Output file.
            return True, content, (written_paths[-1] if written_paths else _guess_latest_output(today))

        # Execute each tool call, append results
        messages.append(msg)
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            handler = TOOL_DISPATCH.get(name)
            if handler is None:
                result = "unknown tool: " + name
            else:
                try:
                    result = handler(args)
                    if name == "write_output_file":
                        m_written = re.match(r"^wrote:\s+(.+?)\s+\(\d+ bytes\)$", result.strip())
                        if m_written:
                            written_paths.append(m_written.group(1))
                    elif name == "move_file":
                        m_moved = re.match(r"^moved:\s+.+?\s+->\s+(.+)$", result.strip())
                        if m_moved:
                            written_paths.append(m_moved.group(1))
                except Exception as exc:
                    result = "tool error: %s" % exc
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": result,
            })

    return False, "exceeded %d iterations" % max_iterations, ""


def _guess_latest_output(today: str) -> str:
    """Return the relative path of the most recent file written to Hermes Output/ today."""
    if not OUTPUT_DIR.exists():
        return ""
    candidates = []
    for f in OUTPUT_DIR.iterdir():
        if not f.is_file() or not f.name.startswith(today):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, f))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return str(candidates[0][1].relative_to(VAULT_ROOT))


# ----------------------- kanban push helper ---------------------------------

def push_to_kanban(env: dict) -> str:
    """Invoke cli/hermes_to_kanban.py to create a GitHub Issue for the just-done task.

    The kanban script reads the daily note directly, finds the most recent
    `- [x]` item, and pushes it. So we just call --max 1.
    """
    kanban_script = REPO_ROOT / "cli" / "hermes_to_kanban.py"
    if not kanban_script.exists():
        return "(kanban script not found; skipped)"
    if not env.get("GITHUB_TOKEN"):
        return "(GITHUB_TOKEN missing; skipped)"
    try:
        result = subprocess.run([sys.executable, str(kanban_script), "--max", "1"],
                                capture_output=True, timeout=60, env=os.environ.copy())
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace").strip()
        return "kanban push failed: " + result.stderr.decode("utf-8", "replace").strip()[:200]
    except Exception as exc:
        return "kanban push error: %s" % exc


def subprocess_run(argv: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, timeout=60, env=os.environ.copy())


# ----------------------- main loop -----------------------------------------

def process_one(today: str, env: dict, push_kanban: bool) -> str:
    api_key = env.get("MINIMAX_API_KEY", "")
    if not api_key:
        return "ERROR: MINIMAX_API_KEY missing in %s" % ENV_PATH
    note_path = find_today_note(today)
    if not note_path.exists():
        return "ERROR: daily note not found: %s" % note_path
    try:
        note_text = _read_daily_note_with_retry(note_path)
    except OSError as exc:
        return "ERROR: read daily note failed: %s" % exc
    nxt = next_open_item(note_text)
    if nxt is None:
        return "no_open_items"
    line_idx, task_text = nxt
    cleaned_text, max_seconds, max_iterations = parse_effort_hint(task_text)
    if max_seconds > 0 or max_iterations > 0:
        eff_s = max_seconds if max_seconds > 0 else MAX_WALL_SECONDS
        eff_i = max_iterations if max_iterations > 0 else MAX_LOOP_ITERATIONS
        print(
            "[worker] processing line %d: %s (budget: %ss, %si)"
            % (line_idx + 1, cleaned_text[:80], eff_s, eff_i)
        )
    else:
        print("[worker] processing line %d: %s" % (line_idx + 1, task_text[:80]))
    # mark_in_progress preserves the original (with hint) line so the user
    # sees their budget annotation while the task is running.
    mark_in_progress(note_path, line_idx, task_text)
    ok, summary, output_path = run_task(
        cleaned_text, today, api_key,
        max_seconds=max_seconds, max_iterations=max_iterations,
    )
    if not ok:
        mark_open(note_path, line_idx, task_text)
        return "failed: " + summary
    if not output_path:
        # Fall back to a re-derive: any new file in Hermes Output starting with today
        output_path = _guess_latest_output(today)
    mark_done(note_path, line_idx, task_text, output_path or "(no output file written)")
    print("[worker] done -> %s" % output_path)
    if push_kanban:
        kanban_msg = push_to_kanban(env)
        print("[worker] kanban: " + kanban_msg)
    return "ok: " + output_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--no-kanban", action="store_true", help="Skip the GitHub Issues push even if GITHUB_TOKEN is set.")
    parser.add_argument("--loop", action="store_true", help="Run in polling mode, processing items every --interval seconds.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval seconds (default 60).")
    parser.add_argument("--max-seconds", type=int, default=None,
                        help="Override default per-task wall-clock budget (seconds). Per-line [NNNs] hints still win.")
    parser.add_argument("--max-iter", type=int, default=None,
                        help="Override default per-task LLM iteration budget. Per-line [NNNi] hints still win.")
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    today = args.date or datetime.now().strftime("%Y-%m-%d")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.max_seconds is not None:
        global MAX_WALL_SECONDS
        MAX_WALL_SECONDS = args.max_seconds
    if args.max_iter is not None:
        global MAX_LOOP_ITERATIONS
        MAX_LOOP_ITERATIONS = args.max_iter

    if not args.loop:
        result = process_one(today, env, push_kanban=not args.no_kanban)
        print(result)
        # exit 0 for handled outcomes (ok, no_open_items, failed); non-zero only on
        # unexpected errors (missing API key, daily note missing/unreadable). A
        # failed task is still a handled outcome; the checkbox is restored to
        # open so the next tick can retry it.
        if result.startswith(("ok", "no_open_items", "failed:")):
            return 0
        return 1

    # Polling loop
    print("[worker] starting polling loop, interval=%ds, date=%s" % (args.interval, today))
    try:
        while True:
            try:
                result = process_one(today, env, push_kanban=not args.no_kanban)
                if result == "no_open_items":
                    pass  # silent when idle
                else:
                    print("[worker] tick: " + result)
            except Exception as exc:
                print("[worker] tick error: %s" % exc)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[worker] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
