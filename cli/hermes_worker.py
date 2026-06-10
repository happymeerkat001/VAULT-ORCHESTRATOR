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
    make_directory, move_file, write_output_file

Stdlib only. No build, no tests, no pip deps. Reads GITHUB_TOKEN, MINIMAX_API_KEY
from .env (repo root).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
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


WRITABLE_ROOTS = ("Hermes Output", "z.Ingestion")


def safe_writable_path(raw: str) -> Path:
    """Like safe_path, but additionally rejects paths inside protected zones
    (e.g. Daily Notes) where the LLM must never write.

    Allowed write destinations: Hermes Output/, z.Ingestion/ — these are the
    zones the user has explicitly approved for Hermes-driven reorganisation.
    """
    p = safe_path(raw)
    rel = p.relative_to(VAULT_ROOT.resolve()).as_posix()
    for allowed in WRITABLE_ROOTS:
        if rel == allowed or rel.startswith(allowed + "/"):
            return p
    raise ValueError(
        "writable path '%s' is not under an allowed root (%s)"
        % (rel, ", ".join(WRITABLE_ROOTS))
    )


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
            "description": "Write text to a file inside the vault (overwrites if exists). Use for the final task deliverable in Hermes Output/.",
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
        m_open = re.match(r"^- \[ \]\s+(.+)$", raw.strip())
        if m_done:
            items.append((k, "done", m_done.group(1)))
        elif m_prog:
            items.append((k, "in_progress", m_prog.group(1)))
        elif m_open:
            items.append((k, "open", m_open.group(1)))
    return start, end, items


def next_open_item(note_text: str) -> tuple[int, str] | None:
    """Return (line_index, raw_task_text) of the next item to work on.

    Prefers in-progress (`- [~]`) items first (crash recovery), then open
    (`- [ ]`) items. Returns None if the section is empty or all done.
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


# ----------------------- daily-note mutation --------------------------------

def mark_in_progress(note_path: Path, line_idx: int, task_text: str) -> None:
    lines = note_path.read_text(encoding="utf-8").splitlines()
    if line_idx < len(lines):
        lines[line_idx] = "- [~] " + task_text + "  _(running)_"
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mark_done(
    note_path: Path,
    line_idx: int,
    original_text: str,
    output_relpath: str,
) -> None:
    lines = note_path.read_text(encoding="utf-8").splitlines()
    if line_idx < len(lines):
        lines[line_idx] = (
            "- [x] " + original_text + "  _(→ see " + output_relpath + ")_"
        )
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mark_failed(note_path: Path, line_idx: int, original_text: str, reason: str) -> None:
    """Leave the item open but append a failure note for human attention."""
    lines = note_path.read_text(encoding="utf-8").splitlines()
    if line_idx < len(lines):
        lines[line_idx] = "- [ ] " + original_text + "  _(failed: " + reason[:120] + ")_"
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------- LLM tool-use loop ---------------------------------

SYSTEM_PROMPT = (
    "You are Hermes, an autonomous task worker. The user added an unchecked "
    "task to today's daily note under '## Hermes-to-do 🪶'. Your job is to "
    "do that task end-to-end using the tools provided.\n\n"
    "Rules:\n"
    "1. Plan internally first; do not narrate every step to the user.\n"
    "2. Use tools to gather context, perform actions, and verify the result.\n"
    "3. You may READ from any folder inside the vault. You may WRITE only to "
    "'Hermes Output/' and 'z.Ingestion/'. Other folders (especially 'Daily Notes/') "
    "are protected: the worker script manages the daily note on your behalf.\n"
    "4. When the work is done, write a final markdown deliverable to "
    "'Hermes Output/' using write_output_file. The file name MUST be "
    "'YYYY-MM-DD <safe-task-name>.md' where YYYY-MM-DD matches today's date.\n"
    "5. After writing the deliverable, your final assistant message should "
    "be plain text: a 2-4 sentence summary of what you did, with the path "
    "of the deliverable file. Do not call any more tools after writing it.\n"
    "6. NEVER add new items to the Hermes-to-do section. NEVER edit the daily "
    "note. The worker script will mark the task done and append a link.\n"
    "7. If the task cannot be done with the available tools, write a brief "
    "deliverable explaining what was attempted and why it failed, then "
    "return the summary as in rule 5."
)


def call_minimax(messages: list[dict], api_key: str) -> dict:
    body = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }).encode("utf-8")
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


def run_task(task_text: str, today: str, api_key: str) -> tuple[bool, str, str]:
    """Run a single task to completion. Returns (ok, summary, output_path)."""
    user_msg = (
        "Today's date: %s\n"
        "Vault root: %s\n"
        "Task: %s\n\n"
        "Begin. Use tools to do the work, then write your deliverable to "
        "Hermes Output/%s <safe-name>.md, then reply with a 2-4 sentence summary."
    ) % (today, VAULT_ROOT, task_text, today)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    start = time.time()
    for iteration in range(MAX_LOOP_ITERATIONS):
        if time.time() - start > MAX_WALL_SECONDS:
            return False, "timed out after %ds" % MAX_WALL_SECONDS, ""

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
            # Final answer. The deliverable should already be on disk; return the summary.
            return True, content, _guess_latest_output(today)

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
                except Exception as exc:
                    result = "tool error: %s" % exc
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": result,
            })

    return False, "exceeded %d iterations" % MAX_LOOP_ITERATIONS, ""


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
        note_text = _read_text_with_retry(note_path)
    except OSError as exc:
        return "ERROR: read daily note failed: %s" % exc
    nxt = next_open_item(note_text)
    if nxt is None:
        return "no_open_items"
    line_idx, task_text = nxt
    print("[worker] processing line %d: %s" % (line_idx + 1, task_text[:80]))
    mark_in_progress(note_path, line_idx, task_text)
    ok, summary, output_path = run_task(task_text, today, api_key)
    if not ok:
        mark_failed(note_path, line_idx, task_text, summary)
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
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    today = args.date or datetime.now().strftime("%Y-%m-%d")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not args.loop:
        result = process_one(today, env, push_kanban=not args.no_kanban)
        print(result)
        return 0 if result.startswith(("ok", "no_open_items")) else 1

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
