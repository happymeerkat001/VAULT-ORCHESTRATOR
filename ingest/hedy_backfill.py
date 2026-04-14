#!/usr/bin/env python3
"""
hedy_backfill.py — Fetches historical Hedy AI sessions and appends them to
date-matched Obsidian daily notes as structured Markdown.

One-time setup:
  Ensure ~/.config/vault-orchestrator/google_credentials contains:
    { "hedy_api_key": "<your_key>", ... }

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/hedy_backfill.py

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
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
CREDENTIALS_PATH = Path("~/.config/vault-orchestrator/google_credentials").expanduser()
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Learning Root"
).expanduser()
LOCAL_TIMEZONE = "America/Los_Angeles"

HEDY_BASE_URL = "https://api.hedy.bot"
HEDY_SESSIONS_URL = "https://api.hedy.bot/sessions?limit=100"
SECTION_HEADER = "## Hedy AI"
SESSION_PREFIX = "### "
KEYWORD_MAP: dict[str, str] = {
    "assurance relay": "[[Assurance Relay LLC]]",
    "real estate": "[[Real Estate]]",
    "python": "[[Python]]",
    "obsidian": "[[Obsidian]]",
    "padsplit": "[[PadSplit]]",
}
# ─────────────────────────────────────────────────────────────────────────────


def today_local() -> str:
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials not found: {CREDENTIALS_PATH}\n"
            'Add {"hedy_api_key": "<key>"} to that file.'
        )
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        creds = json.load(f)
    if not creds.get("hedy_api_key"):
        raise ValueError(f"Missing 'hedy_api_key' in {CREDENTIALS_PATH}")
    return creds


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
        return raw.get("data", raw)
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
                detailed.append(s)
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


def apply_links(text: str) -> tuple[str, list[str]]:
    linked = text
    tags: set[str] = set()

    for key in sorted(KEYWORD_MAP, key=len, reverse=True):
        pattern = re.compile(rf"(?i)\b{re.escape(key)}\b")
        mapped_value = KEYWORD_MAP[key]
        linked, count = pattern.subn(mapped_value, linked)
        if count:
            tags.add(key.lower().replace(" ", "-"))

    return linked, sorted(tags)


def _item_text(item: object) -> str:
    """Extract string from a Hedy list item (may be str or dict)."""
    if isinstance(item, dict):
        return (item.get("text") or item.get("title") or item.get("content") or "").strip()
    return str(item).strip()


def format_session(session: dict) -> str:
    """Convert one Hedy session detail dict to a Markdown block."""
    title = (session.get("title") or "Untitled Session").strip()
    session_type = (session.get("session_type") or "").replace("_", " ")
    duration = session.get("duration")
    topic_name = ((session.get("topic") or {}).get("name") or "").strip()

    recap = (session.get("recap") or "").strip()
    meeting_minutes = (session.get("meeting_minutes") or "").strip()
    user_todos = [_item_text(i) for i in (session.get("user_todos") or []) if _item_text(i)]
    highlights = [_item_text(i) for i in (session.get("highlights") or []) if _item_text(i)]

    tags: set[str] = set()
    lines = [f"{SESSION_PREFIX}{title}"]

    meta = " · ".join(p for p in [
        session_type,
        f"{duration} min" if duration else "",
        topic_name,
    ] if p)
    if meta:
        lines.append(f"*{meta}*")
    lines.append("")

    if recap:
        linked, found_tags = apply_links(recap)
        tags.update(found_tags)
        lines.append("**Recap:**")
        lines.append(linked)
        lines.append("")

    if meeting_minutes:
        linked, found_tags = apply_links(meeting_minutes)
        tags.update(found_tags)
        lines.append("**Meeting Notes:**")
        lines.append(linked)
        lines.append("")

    if user_todos:
        lines.append("**Action Items:**")
        for item in user_todos:
            linked, found_tags = apply_links(item)
            tags.update(found_tags)
            lines.append(f"- {linked}")
        lines.append("")

    if highlights:
        lines.append("**Highlights:**")
        for h in highlights:
            linked, found_tags = apply_links(h)
            tags.update(found_tags)
            lines.append(f"- {linked}")
        lines.append("")

    if tags:
        lines.append(" ".join(f"#{t}" for t in sorted(tags)))
        lines.append("")

    return "\n".join(lines)


def get_existing_session_titles(note_path: Path) -> set[str]:
    """Return set of session titles already written (idempotency guard)."""
    if not note_path.exists():
        return set()
    titles: set[str] = set()
    for line in note_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(SESSION_PREFIX):
            titles.add(line[len(SESSION_PREFIX):].strip())
    return titles


def append_to_note(note_path: Path, text: str) -> None:
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(text)


def write_error_callout(note_path: Path, message: str) -> None:
    callout = (
        f"\n> [!ERROR] Hedy Sync Failed: {message}. "
        f"Check `~/.config/vault-orchestrator/google_credentials` or run manually.\n"
    )
    append_to_note(note_path, callout)


def main() -> None:
    error_note_path = VAULT_PATH / f"{today_local()}.md"
    print("[hedy_backfill] starting full-date backfill")

    # 1. Load credentials
    try:
        creds = load_credentials()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        write_error_callout(error_note_path, str(exc))
        sys.exit(1)

    # 2. Fetch sessions
    try:
        all_sessions = fetch_sessions(creds["hedy_api_key"])
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} from Hedy API"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_callout(error_note_path, msg)
        sys.exit(1)
    except Exception as exc:
        msg = f"Network/parse error: {exc}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        write_error_callout(error_note_path, msg)
        sys.exit(1)

    sessions_by_date: dict[str, list[dict]] = {}
    for session in all_sessions:
        date = session_date(session)
        if not date:
            continue
        sessions_by_date.setdefault(date, []).append(session)

    if not sessions_by_date:
        print("[hedy_backfill] No dated sessions found. Nothing to append.")
        return

    for date in sorted(sessions_by_date):
        note_path = VAULT_PATH / f"{date}.md"
        dated_sessions = sessions_by_date[date]

        existing_titles = get_existing_session_titles(note_path)
        new_sessions = [
            s for s in dated_sessions
            if (s.get("title") or s.get("name") or "Untitled Session").strip()
            not in existing_titles
        ]

        if not new_sessions:
            continue

        has_section = (
            note_path.exists()
            and SECTION_HEADER in note_path.read_text(encoding="utf-8")
        )
        output = ""
        if not has_section:
            output += f"\n{SECTION_HEADER} — {date}\n\n"

        for session in new_sessions:
            output += format_session(session)

        append_to_note(note_path, output)
        print(f"[hedy_backfill] {date}: {len(new_sessions)} new session(s) → {note_path}")


if __name__ == "__main__":
    main()
