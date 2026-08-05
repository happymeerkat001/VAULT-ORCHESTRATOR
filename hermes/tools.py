"""Sandboxed file and public-web tools exposed to the Hermes worker."""

import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from . import vaultio

VAULT_ROOT = Path(".")
WEB_TOOL_TIMEOUT = 30
WEB_FETCH_MAX_CHARS = 40_000
WEB_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
MAX_READ_FILE_LINES = 300
safe_path = vaultio.safe_path
safe_writable_path = vaultio.safe_writable_path
_read_text_with_iCloud_retry = vaultio._read_text_with_iCloud_retry
_is_icloud_lock_error = vaultio._is_icloud_lock_error
_write_text_with_retry = vaultio._write_text_with_retry
_ICLOUD_LOCK_SENTINEL = vaultio._ICLOUD_LOCK_SENTINEL

def configure(*, vault_root: Path, web_tool_timeout: int, web_fetch_max_chars: int) -> None:
    globals().update(VAULT_ROOT=vault_root, WEB_TOOL_TIMEOUT=web_tool_timeout, WEB_FETCH_MAX_CHARS=web_fetch_max_chars)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the UTF-8 text of a file inside the vault. Returns up to N lines (clamped to a hard ceiling of 300 lines per call to protect the context budget; if you need more, re-read with a tighter scope).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the vault, or absolute path inside the vault."},
                    "max_lines": {"type": "integer", "description": "Cap on lines returned (default 200, hard ceiling 300)."},
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


# Hard ceiling on read_file's max_lines. The model can ask for more, but
# the worker clamps to 300 lines per read so a single transcript folder
# can't blow out the LLM context window. Pairs with the context-trim
# guard in run_task: this stops a single tool call from overshooting,
# the trim guard catches sustained growth across many calls. 300 lines
# of prose is roughly 12-15k tokens; well below the per-call budget and
# enough for any sensible summary.
MAX_READ_FILE_LINES = 300


def tool_read_file(args: dict) -> str:
    p = safe_path(args["path"])
    if not p.exists():
        return "(file does not exist)"
    if p.is_dir():
        return "(path is a directory; use list_directory)"
    try:
        text = _read_text_with_iCloud_retry(p)
    except OSError as exc:
        if _is_icloud_lock_error(exc):
            # Self-heal: the file is iCloud-locked and our retry budget
            # did not free it. Return a soft message so the LLM keeps
            # working on other files in the run and circles back to
            # this one in a later iteration. Raising here would abort
            # the whole task on a single cold file.
            return _ICLOUD_LOCK_SENTINEL + " (%s)" % exc
        raise
    requested = int(args.get("max_lines") or 200)
    limit = min(max(1, requested), MAX_READ_FILE_LINES)
    lines = text.splitlines()
    if len(lines) > limit:
        return "\n".join(lines[:limit]) + "\n…(truncated at %d lines; raise max_lines or re-read with a wider scope to see more)" % limit
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
