#!/usr/bin/env python3
"""
hedy_sync.py — Fetches today's Hedy AI sessions and appends them to the
Obsidian daily note as structured Markdown.

One-time setup:
  Add HEDY_AI_API_KEY to /Users/leon/Documents/Code/vault-orchestrator/.env
  or set hedy_api_key in ~/.config/vault-orchestrator/google_credentials

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/hedy_sync.py

Crontab (11:45 PM daily):
  mkdir -p /Users/leon/Library/Logs
  45 23 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/hedy_sync.py >> /Users/leon/Library/Logs/hedy_sync.log 2>&1

No external dependencies — stdlib only.
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    from ingest.hedy_common import (
        append_to_note,
        build_sessions_output,
        ensure_daily_note_link,
        ensure_transcript_link,
        get_existing_session_titles,
        hedy_note_path,
        transcript_note_path,
        write_transcript_note,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ingest.hedy_common import (
        append_to_note,
        build_sessions_output,
        ensure_daily_note_link,
        ensure_transcript_link,
        get_existing_session_titles,
        hedy_note_path,
        transcript_note_path,
        write_transcript_note,
    )

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
CREDENTIALS_PATH = Path("~/.config/vault-orchestrator/google_credentials").expanduser()
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOCAL_TIMEZONE = "America/Chicago"

HEDY_BASE_URL = "https://api.hedy.bot"
HEDY_SESSIONS_URL = "https://api.hedy.bot/sessions?limit=10"
# ─────────────────────────────────────────────────────────────────────────────


def today_local() -> str:
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def load_hedy_api_key() -> str:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^\s*HEDY_AI_API_KEY\s*=\s*"?([^"]+)"?\s*$', line)
            if match:
                return match.group(1)

    if CREDENTIALS_PATH.exists():
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            creds = json.load(f)
        if creds.get("hedy_api_key"):
            return creds["hedy_api_key"]

    raise ValueError("No Hedy API key found in .env or google_credentials")


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _hedy_get(api_key: str, url: str) -> dict | list:
    """Authenticated GET to Hedy API. Returns parsed JSON."""
    req = urllib.request.Request(
        url, method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_session_detail(api_key: str, session_id: str) -> dict:
    """GET /sessions/{id} — returns full session with recap, meeting_minutes, user_todos, highlights."""
    raw = _hedy_get(api_key, f"{HEDY_BASE_URL}/sessions/{session_id}")
    if isinstance(raw, dict):
        return raw.get("data", raw)  # unwrap {success, data} envelope if present
    return {}


def fetch_sessions(api_key: str) -> list[dict]:
    """Fetch session list then enrich each entry with full detail."""
    raw = _hedy_get(api_key, HEDY_SESSIONS_URL)
    summaries: list[dict] = raw if isinstance(raw, list) else next(
        (v for v in raw.values() if isinstance(v, list)), []
    )
    if not summaries:
        raise RuntimeError(f"Unexpected response shape: {json.dumps(raw)[:300]}")

    detailed = []
    for s in summaries:
        sid = s.get("sessionId") or s.get("id")
        if sid:
            try:
                detail = fetch_session_detail(api_key, sid)
                detailed.append(detail if detail else s)
            except Exception:
                detailed.append(s)  # fall back to summary-only on error
        else:
            detailed.append(s)
    return detailed


def session_date(session: dict) -> str:
    """Extract YYYY-MM-DD from whichever date field exists."""
    for field in ("date", "created_at", "start_time", "startTime", "timestamp"):
        value = session.get(field, "")
        if value and len(value) >= 10:
            return value[:10]  # slice ISO8601 to YYYY-MM-DD
    return ""


def write_error_callout(note_path: Path, message: str) -> None:
    callout = (
        f"\n> [!ERROR] Hedy Sync Failed: {message}. "
        f"Check `~/.config/vault-orchestrator/google_credentials` or run manually.\n"
    )
    append_to_note(note_path, callout)


def inject_success_callout(note_path: Path, count: int) -> None:
    """Inject a success callout near the top of the note after YAML frontmatter.
    Idempotent — skips if the marker is already present."""
    callout_marker = "> [!success] 🎙️ **Hedy Sync:**"
    callout = f"{callout_marker} {count} new session(s) appended below."

    content = note_path.read_text(encoding="utf-8")
    if callout_marker in content:
        return  # already injected this run or a previous run

    lines = content.splitlines(keepends=True)

    # Find insertion point: right after closing `---` of YAML frontmatter, else line 0
    insert_pos = 0
    if lines and lines[0].rstrip() == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip() == "---":
                insert_pos = i + 1
                break

    # Blank line before callout when placed after frontmatter; callout at top otherwise
    injection = f"\n{callout}\n\n" if insert_pos > 0 else f"{callout}\n\n"
    lines.insert(insert_pos, injection)
    note_path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    today = today_local()
    note_path = hedy_note_path(today)
    print(f"[hedy_sync] date={today}  note={note_path}")

    # 1. Load credentials
    try:
        api_key = load_hedy_api_key()
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        write_error_callout(note_path, str(exc))
        sys.exit(1)

    # 2. Fetch sessions
    try:
        all_sessions = fetch_sessions(api_key)
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} from Hedy API"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_callout(note_path, msg)
        sys.exit(1)
    except Exception as exc:
        msg = f"Network/parse error: {exc}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_callout(note_path, msg)
        sys.exit(1)

    # 3. Filter to today's sessions only
    todays = [s for s in all_sessions if session_date(s) == today]
    print(f"[hedy_sync] total={len(all_sessions)}  today={len(todays)}")

    if not todays:
        print(f"[hedy_sync] No Hedy sessions for {today}. Nothing to append.")
        return

    ensure_daily_note_link(today)

    # 4. Idempotency — skip sessions already written
    existing_titles = get_existing_session_titles(note_path)
    new_sessions = [
        s for s in todays
        if (s.get("title") or s.get("name") or "Untitled Session").strip()
        not in existing_titles
    ]

    if not new_sessions:
        wrote_transcript = write_transcript_note(todays, today)
        if wrote_transcript or transcript_note_path(today).exists():
            ensure_transcript_link(note_path, today)
        print(f"[hedy_sync] All {len(todays)} session(s) already present. Nothing to do.")
        return

    # 5. Build output block
    output = build_sessions_output(note_path, new_sessions, today)

    # 6. Append
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(output)
    print(f"[hedy_sync] Appended {len(new_sessions)} new session(s) to {note_path}")

    # 7. Write transcripts to Hedy-AI/YYYY-MM-DD.md
    wrote_transcript = write_transcript_note(todays, today)
    if wrote_transcript or transcript_note_path(today).exists():
        ensure_transcript_link(note_path, today)

    # 8. Inject success callout near top of note
    inject_success_callout(note_path, len(new_sessions))


if __name__ == "__main__":
    main()
