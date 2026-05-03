#!/usr/bin/env python3
"""
briefing_sync.py — Fetches today's Google Calendar + Gmail, generates a
daily briefing via MiniMax AI, and writes it to Obsidian Daily Notes.

One-time setup:
  1. mkdir -p ~/.config/vault-orchestrator
  2. cat > ~/.config/vault-orchestrator/google_credentials << 'EOF'
     {
       "minimax_api_key": "<MINIMAX_API_KEY>",
       "google_client_id": "<GOOGLE_CLIENT_ID>",
       "google_client_secret": "<GOOGLE_CLIENT_SECRET>",
       "google_redirect_uri": "<GOOGLE_REDIRECT_URI>",
       "google_refresh_token": "<GOOGLE_REFRESH_TOKEN>"
     }
     EOF
  3. chmod 600 ~/.config/vault-orchestrator/google_credentials
  4. python3 briefing_sync.py          # verify manually
  5. Use /Users/leon/.claude/scripts/run-briefing.sh from the user LaunchAgent
     at ~/Library/LaunchAgents/com.leon.briefing.daily.plist for the scheduled run.
     The old direct cron->python path is not the supported setup on macOS because
     Full Disk Access restrictions can block that execution path.

No external dependencies — stdlib only.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
CREDENTIALS_PATH = Path("~/.config/vault-orchestrator/google_credentials").expanduser()
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator"
).expanduser()
DAILY_NOTES_PATH = VAULT_PATH / "Daily Notes"
LOCAL_TIMEZONE = "America/Chicago"
MAX_STARRED_EMAILS = 3
BRIEFING_HEADER = "## Morning Briefing ☀️"
STANDING_TO_THINK_ITEMS = [
    "Huberman 5",
    "No cell phone",
]
# ─────────────────────────────────────────────────────────────────────────────

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"
GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GOOGLE_GMAIL_LIST_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"

EMAIL_SYSTEM_PROMPT = (
    "You are preparing a concise daily briefing in markdown for the user. "
    "Output ONLY the content for two sections. Do not mention Slack anywhere. "
    "Do not wrap output in code fences. Do not include a title or date header. "
    "All data is already provided in the JSON below — use only this data.\n\n"
    "Section 1: '# To-Think 🧠' — reflections, learning items, ideas to ponder. "
    "Each item is a markdown checkbox: '- [ ] item'.\n\n"
    "If 'standingToThinkItems' is present, include every listed item exactly once "
    "in To-Think as unchecked checklist items. Keep those items verbatim.\n\n"
    "Section 2: '## To-Do ✅' — actionable tasks derived from calendar and emails. "
    "Each item is a markdown checkbox: '- [ ] item'.\n\n"
    "After To-Do, add '## Calendar 📅' listing events as bullets (not checkboxes) "
    "with times in 12-hour format. The 'calendarDays' field tells you how many days "
    "of events are included. Group events by date with a bold date label "
    "(e.g. **Sunday 04/26**, **Monday 04/27**) when calendarDays > 1.\n\n"
    "If no calendar events exist, write exactly: 'No events scheduled.'\n\n"
    "Then add '## Email Highlights 📧' with a one-line summary header "
    "'**Starred:** N emails' followed by checklist items. List only the top 3 starred emails "
    "present in the data — do not add any extras. If no starred emails exist, "
    "write exactly: 'No starred emails.'\n\n"
    "If 'rolloverFromYesterday' is present, include those unchecked items "
    "in the briefing — do not drop them. Place all rollover items in the To-Do section; "
    "never place rollover items in Calendar or Email Highlights.\n\n"
    "Keep it concise. No prose paragraphs. Checkboxes only."
)

EMAIL_USER_PROMPT_LINES = [
    "Review the data below and produce the briefing sections.",
    "Output markdown only — no code fences, no extra headers.",
    "",
    "Raw JSON Data:",
]


def today_local() -> str:
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def get_calendar_bounds() -> tuple[str, str, int]:
    """Return ISO8601 start/end for calendar window and number of days.

    Sunday: 7 days, Wednesday: 4 days, all other days: 2 days (today + tomorrow).
    """
    if _HAS_ZONEINFO:
        tz = ZoneInfo(LOCAL_TIMEZONE)
        now = datetime.now(tz=tz)
    else:
        now = datetime.now()

    weekday = now.weekday()  # 0=Mon … 6=Sun
    if weekday == 6:      # Sunday
        lookahead = 7
    elif weekday == 2:    # Wednesday
        lookahead = 4
    else:
        lookahead = 2

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=lookahead)).replace(
        hour=23, minute=59, second=59, microsecond=999000
    ) - timedelta(days=1)

    if _HAS_ZONEINFO:
        start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        start_utc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    return start_utc, end_utc, lookahead


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}\n"
            "Create it with keys: minimax_api_key, google_client_id, "
            "google_client_secret, google_redirect_uri, google_refresh_token"
        )
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        creds = json.load(f)

    required = [
        "minimax_api_key", "google_client_id", "google_client_secret",
        "google_redirect_uri", "google_refresh_token",
    ]
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise ValueError(f"Missing credential keys: {', '.join(missing)}")
    return creds


def refresh_access_token(creds: dict) -> str:
    """Exchange refresh token for a short-lived access token."""
    payload = urllib.parse.urlencode({
        "client_id": creds["google_client_id"],
        "client_secret": creds["google_client_secret"],
        "refresh_token": creds["google_refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    return data["access_token"]


def _get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_calendars(access_token: str) -> list[str]:
    calendar_ids = []
    page_token = None

    while True:
        params = {
            "showDeleted": "false",
            "showHidden": "false",
        }
        if page_token:
            params["pageToken"] = page_token

        data = _get_json(
            f"{GOOGLE_CALENDAR_LIST_URL}?{urllib.parse.urlencode(params)}",
            access_token,
        )
        for calendar in data.get("items") or []:
            if calendar.get("selected") is False:
                continue
            calendar_id = calendar.get("id")
            if calendar_id:
                calendar_ids.append(calendar_id)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return calendar_ids


def _event_start_sort_key(event: dict) -> str:
    start = event.get("start") or {}
    return start.get("dateTime") or start.get("date") or ""


def fetch_calendar_events(access_token: str) -> tuple[dict, int]:
    start, end, lookahead = get_calendar_bounds()
    calendar_ids = fetch_all_calendars(access_token)
    events = []

    for calendar_id in calendar_ids:
        page_token = None
        encoded_calendar_id = urllib.parse.quote(calendar_id, safe="")

        while True:
            params = {
                "timeMin": start,
                "timeMax": end,
                "singleEvents": "true",
                "orderBy": "startTime",
            }
            if page_token:
                params["pageToken"] = page_token

            data = _get_json(
                (
                    f"{GOOGLE_CALENDAR_BASE_URL}/{encoded_calendar_id}/events?"
                    f"{urllib.parse.urlencode(params)}"
                ),
                access_token,
            )
            events.extend(data.get("items") or [])

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    events.sort(key=_event_start_sort_key)
    return {"items": events}, lookahead


def fetch_starred_emails(access_token: str) -> dict:
    params = urllib.parse.urlencode({
        "q": "is:starred",
        "maxResults": MAX_STARRED_EMAILS,
    })
    list_data = _get_json(f"{GOOGLE_GMAIL_LIST_URL}?{params}", access_token)
    messages = list_data.get("messages") or []

    if not messages:
        return {"messages": []}

    detailed = []
    for msg in messages:
        meta_params = urllib.parse.urlencode({
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date"],
        }, doseq=True)
        msg_data = _get_json(
            f"{GOOGLE_GMAIL_LIST_URL}/{msg['id']}?{meta_params}", access_token
        )
        detailed.append(msg_data)

    return {
        "resultSizeEstimate": len(detailed),
        "messages": detailed,
    }


def generate_briefing(payload: dict, minimax_api_key: str) -> str:
    user_content = "\n".join(EMAIL_USER_PROMPT_LINES) + "\n" + json.dumps(payload, indent=2)

    body = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [
            {"role": "system", "content": EMAIL_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        MINIMAX_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {minimax_api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError(f"MiniMax returned no content: {data}")
    # Strip <think>...</think> reasoning blocks leaked by the model
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    return content


def read_text_with_retry(path: Path, attempts: int = 10, delay_s: float = 0.5) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last_exc = exc
            time.sleep(delay_s)
    raise last_exc or OSError(f"Unable to read {path}")


def get_yesterday_unchecked(date_str: str) -> list[str]:
    """Extract unchecked items only from To-Think and To-Do in yesterday's note."""
    if _HAS_ZONEINFO:
        tz = ZoneInfo(LOCAL_TIMEZONE)
        today = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        today = datetime.strptime(date_str, "%Y-%m-%d")
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_path = DAILY_NOTES_PATH / f"{yesterday}.md"

    if not yesterday_path.exists():
        return []

    content = read_text_with_retry(yesterday_path)
    lines = content.splitlines()

    collecting = False
    unchecked: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped in {"# To-Think 🧠", "## To-Do ✅"}:
            collecting = True
            continue
        if collecting and stripped.startswith("## ") and stripped not in {"## To-Do ✅"}:
            collecting = False
            continue
        if collecting and re.match(r"^- \[ \] .+$", line):
            unchecked.append(line)

    return unchecked


def build_note_preamble(date_str: str) -> str:
    current = datetime.strptime(date_str, "%Y-%m-%d")
    prev_date = (current - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (current + timedelta(days=1)).strftime("%Y-%m-%d")
    return (
        "---\n"
        "tags:\n"
        "  - 📓\n"
        "---\n"
        f"Days:[[Daily Notes/{prev_date} | Yesterday]] <== [[Daily Notes/{date_str}]] ==> "
        f"[[Daily Notes/{next_date}|Tomorrow]]\n"
    )


def has_note_preamble(content: str) -> bool:
    head = "\n".join(content.splitlines()[:20])
    return head.startswith("---\n") and "tags:" in head and "Days:[[" in head


def write_briefing(date_str: str, markdown: str) -> Path:
    out_path = DAILY_NOTES_PATH / f"{date_str}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    preamble = build_note_preamble(date_str)

    if out_path.exists():
        existing = read_text_with_retry(out_path)
        if not has_note_preamble(existing):
            existing = f"{preamble}\n{existing.lstrip()}"
            out_path.write_text(existing, encoding="utf-8")
        # Check for the main header or any partial briefing artifacts
        briefing_markers = (BRIEFING_HEADER, "## Calendar 📅", "## Email Highlights 📧", "## Today's Focus 🧐")
        if any(marker in existing for marker in briefing_markers):
            print(f"[briefing_sync] briefing content already present in {out_path.name}, skipping.")
            return out_path
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"\n{markdown}")
    else:
        out_path.write_text(f"{preamble}\n{markdown}", encoding="utf-8")

    return out_path


def main() -> None:
    today = today_local()
    print(f"[briefing_sync] date={today}")
    print(f"[briefing_sync] output_path={DAILY_NOTES_PATH / f'{today}.md'}")

    # 1. Load credentials
    try:
        creds = load_credentials()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    # 2. Refresh Google OAuth2 access token
    try:
        access_token = refresh_access_token(creds)
    except Exception as exc:
        print(f"[ERROR] Token refresh failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # 3. Fetch data in sequence (stdlib has no async — keep it simple)
    try:
        calendar_data, lookahead = fetch_calendar_events(access_token)
        print(f"[briefing_sync] calendar: {len(calendar_data.get('items') or [])} event(s) ({lookahead} day window)")
    except Exception as exc:
        print(f"[ERROR] Calendar fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        email_data = fetch_starred_emails(access_token)
        print(f"[briefing_sync] starred emails: {len(email_data.get('messages') or [])}")
    except Exception as exc:
        print(f"[ERROR] Gmail fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # 4. Gather rollover items from yesterday
    try:
        rollover = get_yesterday_unchecked(today)
    except OSError as exc:
        print(f"[WARN] Could not read yesterday's note for rollover: {exc}", file=sys.stderr)
        rollover = []
    if rollover:
        print(f"[briefing_sync] rollover: {len(rollover)} unchecked item(s) from yesterday")

    # 5. Generate briefing
    payload = {
        "date": today,
        "calendarDays": lookahead,
        "calendar": calendar_data,
        "starredEmails": email_data,
        "standingToThinkItems": STANDING_TO_THINK_ITEMS,
    }
    if rollover:
        payload["rolloverFromYesterday"] = rollover

    try:
        ai_markdown = generate_briefing(payload, creds["minimax_api_key"])
    except Exception as exc:
        print(f"[ERROR] MiniMax generation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # 6. Write to Obsidian Daily Notes
    markdown = f"{BRIEFING_HEADER}\n\n{ai_markdown.strip()}\n"
    try:
        out_path = write_briefing(today, markdown)
        print(f"[briefing_sync] wrote to: {out_path}")
    except OSError as exc:
        print(f"[ERROR] Failed to write file: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
