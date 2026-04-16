#!/usr/bin/env python3
"""
mymemo_sync.py — Syncs today's MyMemo AI digests to Obsidian daily note.

One-time setup:
  1. mkdir -p ~/.config/mymemo
  2. cat > ~/.config/mymemo/credentials << 'EOF'
     {"m_authorization": "<paste JWT here>", "auth0_cookie": "<paste cookie string here>"}
     EOF
  3. python3 mymemo_sync.py          # verify manually
  4. crontab -e, add:
     50 23 * * * /usr/bin/python3 /Users/leon/Documents/Code/mymemo-obsidian/mymemo_sync.py >> /Users/leon/Library/Logs/mymemo_sync.log 2>&1

Token refresh: if you get an ERROR callout in Obsidian, re-capture m_authorization from
DevTools (Network tab → any /api/* request → Headers → m_authorization) and update credentials.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False  # Python < 3.9 — falls back to system local time

# ── CONFIGURATION (edit these) ────────────────────────────────────────────────
CREDENTIALS_PATH = Path("~/.config/mymemo/credentials").expanduser()
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-Orchestrator"
).expanduser()

# Timezone for determining "today". Find yours via: python3 -c "import zoneinfo; print(sorted(zoneinfo.available_timezones()))"
LOCAL_TIMEZONE = "America/Los_Angeles"

# API list type sent in the request payload (controls which memo category is returned).
# NOTE: this is distinct from the item-level `type` field (e.g. 6 = podcast digest).
# Change if you want a different category — 1 returns podcast digests.
API_LIST_TYPE = 1

# Set True to append full transcript (parseContent) instead of short summary (content).
USE_FULL_CONTENT = False

# Fetch up to N items per call; one podcast digest per day so 50 is plenty.
PAGE_SIZE = 50
# ─────────────────────────────────────────────────────────────────────────────

API_URL = "https://app.mymemo.ai/api/memocast/manual-list"
PARTNER_CODE = "Rbe18e92dbfca43edb7b9eee4aef7b9f5"


# ── ANTI-CORRUPTION LAYER: raw API dict → typed object ───────────────────────
@dataclass
class Memo:
    id: int
    title: str
    body: str       # short summary or full transcript depending on USE_FULL_CONTENT
    start_date: str  # "YYYY-MM-DD"


def parse_memos(raw_items: list[dict]) -> list["Memo"]:
    """Convert raw API JSON dicts into Memo dataclasses. Isolates formatting from API shape."""
    result = []
    for item in raw_items:
        body = (item.get("parseContent") or item.get("content") or "") if USE_FULL_CONTENT \
               else (item.get("content") or "")
        result.append(Memo(
            id=item["id"],
            title=(item.get("title") or f"Memo {item['id']}").strip(),
            body=body.strip(),
            start_date=item.get("startDate") or "",
        ))
    return result
# ─────────────────────────────────────────────────────────────────────────────


def today_local() -> str:
    """Return today's date string in LOCAL_TIMEZONE as YYYY-MM-DD."""
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")  # fallback: system local time


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}\n"
            'Create it: {"m_authorization": "<JWT>", "auth0_cookie": "<cookie>"}'
        )
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_raw(credentials: dict) -> dict:
    """POST to MyMemo API. Raises urllib.error.HTTPError on non-2xx."""
    payload = json.dumps({
        "pageIndex": 1,
        "pageSize": PAGE_SIZE,
        "partnerCode": PARTNER_CODE,
        "type": API_LIST_TYPE,
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8",
            "m_authorization": credentials["m_authorization"],
            "cookie": credentials.get("auth0_cookie", ""),
            "origin": "https://app.mymemo.ai",
            "referer": "https://app.mymemo.ai/home",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_memo_block(memo: Memo) -> str:
    return f"\n### {memo.title}\n{memo.body}\n"


def get_existing_titles(note_path: Path) -> set[str]:
    """Return set of '### Title' headings already in today's note (idempotency check)."""
    if not note_path.exists():
        return set()
    titles: set[str] = set()
    for line in note_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            titles.add(line[4:].strip())
    return titles


def append_to_note(note_path: Path, text: str) -> None:
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(text)


def write_error_callout(note_path: Path) -> None:
    """Append a visible Obsidian callout so failures are never silent."""
    callout = (
        "\n> [!ERROR] MyMemo Sync Failed: Token Expired or API Error. "
        "Update `~/.config/mymemo/credentials`.\n"
    )
    append_to_note(note_path, callout)


def main() -> None:
    today = today_local()
    note_path = VAULT_PATH / f"{today}.md"
    print(f"[mymemo_sync] date={today}  note={note_path}")

    # 1. Load credentials
    try:
        credentials = load_credentials()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        write_error_callout(note_path)
        sys.exit(1)

    # 2. Fetch from API
    try:
        raw_response = fetch_raw(credentials)
    except urllib.error.HTTPError as exc:
        print(f"[ERROR] HTTP {exc.code} from API", file=sys.stderr)
        write_error_callout(note_path)
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Network error: {exc}", file=sys.stderr)
        write_error_callout(note_path)
        sys.exit(1)

    # 3. Check for auth error in response body (API returns 200 with code:401 sometimes)
    if raw_response.get("code") in (401, 500) or raw_response.get("msg") not in ("success", None, ""):
        if raw_response.get("code") != 200:
            print(f"[ERROR] API error: {raw_response}", file=sys.stderr)
            write_error_callout(note_path)
            sys.exit(1)

    raw_list = raw_response.get("data", {}).get("list", [])
    if not isinstance(raw_list, list):
        print(f"[ERROR] Unexpected response shape: {raw_response}", file=sys.stderr)
        write_error_callout(note_path)
        sys.exit(1)

    # 4. Filter to today's date only (client-side, timezone-aware)
    todays_raw = [item for item in raw_list if item.get("startDate") == today]
    if not todays_raw:
        print(f"[mymemo_sync] No memos for {today}. Nothing to append.")
        return

    # 5. Anti-corruption parse
    memos = parse_memos(todays_raw)

    # 6. Idempotency: skip titles already written
    existing_titles = get_existing_titles(note_path)
    new_memos = [m for m in memos if m.title not in existing_titles]

    if not new_memos:
        print(f"[mymemo_sync] All {len(memos)} memo(s) already present. Nothing to do.")
        return

    # 7. Build output block
    has_section = note_path.exists() and "## MyMemo" in note_path.read_text(encoding="utf-8")
    output = ""
    if not has_section:
        output += f"\n## MyMemo — {today}\n"
    for memo in new_memos:
        output += format_memo_block(memo)

    # 8. Append
    append_to_note(note_path, output)
    print(f"[mymemo_sync] Appended {len(new_memos)} new memo(s) to {note_path}")


if __name__ == "__main__":
    main()
