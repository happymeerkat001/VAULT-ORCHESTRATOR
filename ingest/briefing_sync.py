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
  5. crontab -e, add (runs 6 AM daily):
     0 6 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/briefing_sync.py >> /Users/leon/Library/Logs/briefing_sync.log 2>&1

No external dependencies — stdlib only.
"""

import json
import sys
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
VAULT_DAILY_NOTES = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Learning Root/Daily Notes"
).expanduser()
LOCAL_TIMEZONE = "America/Los_Angeles"
MAX_EMAIL_RESULTS = 25
# ─────────────────────────────────────────────────────────────────────────────

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GOOGLE_GMAIL_LIST_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"

KEYWORD_FLAGS = [
    "PadSplit", "tenant", "rent", "mortgage", "property",
    "church", "dissertation", "deal", "flip",
]

EMAIL_SYSTEM_PROMPT = (
    "You are preparing a concise daily briefing in markdown for the user. "
    "Review inputs and prioritize actions. Do not mention Slack anywhere."
)

EMAIL_USER_PROMPT_LINES = [
    "Review the data below.",
    "Flag emails containing any of these keywords: "
    + ", ".join(KEYWORD_FLAGS) + ".",
    "Infer the Top 3 priorities for \"Today's Focus\".",
    "Output markdown only.",
    "",
    "Raw JSON Data:",
]


def today_local() -> str:
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def get_today_bounds() -> tuple[str, str]:
    """Return ISO8601 start/end of today in UTC."""
    if _HAS_ZONEINFO:
        tz = ZoneInfo(LOCAL_TIMEZONE)
        now = datetime.now(tz=tz)
    else:
        now = datetime.now()

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999000)

    # Convert to UTC ISO8601
    if _HAS_ZONEINFO:
        start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        start_utc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    return start_utc, end_utc


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


def fetch_calendar_events(access_token: str) -> dict:
    start, end = get_today_bounds()
    params = urllib.parse.urlencode({
        "timeMin": start,
        "timeMax": end,
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    return _get_json(f"{GOOGLE_CALENDAR_URL}?{params}", access_token)


def fetch_unread_emails(access_token: str) -> dict:
    params = urllib.parse.urlencode({
        "q": "is:unread newer_than:1d",
        "maxResults": MAX_EMAIL_RESULTS,
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
        "resultSizeEstimate": list_data.get("resultSizeEstimate"),
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
    return content


def write_briefing(date_str: str, markdown: str) -> Path:
    out_path = VAULT_DAILY_NOTES / f"{date_str} Briefing.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def main() -> None:
    today = today_local()
    print(f"[briefing_sync] date={today}")

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
        calendar_data = fetch_calendar_events(access_token)
        print(f"[briefing_sync] calendar: {len(calendar_data.get('items') or [])} event(s)")
    except Exception as exc:
        print(f"[ERROR] Calendar fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        email_data = fetch_unread_emails(access_token)
        print(f"[briefing_sync] emails: {len(email_data.get('messages') or [])} unread")
    except Exception as exc:
        print(f"[ERROR] Gmail fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # 4. Generate briefing
    payload = {
        "date": today,
        "calendar": calendar_data,
        "unreadEmailsLast24Hours": email_data,
    }

    try:
        ai_markdown = generate_briefing(payload, creds["minimax_api_key"])
    except Exception as exc:
        print(f"[ERROR] MiniMax generation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # 5. Write to Obsidian
    markdown = f"# {today} Briefing\n\n{ai_markdown.strip()}\n"
    try:
        out_path = write_briefing(today, markdown)
        print(f"[briefing_sync] Briefing written to: {out_path}")
    except OSError as exc:
        print(f"[ERROR] Failed to write file: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
