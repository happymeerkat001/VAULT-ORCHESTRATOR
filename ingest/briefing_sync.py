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
       "google_refresh_token": "<GOOGLE_REFRESH_TOKEN>",
       "weatherapi_com_key": "<WEATHERAPI_COM_KEY>",
       "weather_location": "Austin,TX"
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
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault"
).expanduser()
DAILY_NOTES_PATH = VAULT_PATH / "Daily Notes"
LOCAL_TIMEZONE = "America/Chicago"
MAX_STARRED_EMAILS = 3
BRIEFING_HEADER = "## Morning Briefing ☀️"
HERMES_TODO_HEADER = "## Hermes-to-do 🪶"
# ─────────────────────────────────────────────────────────────────────────────

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"
GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GOOGLE_GMAIL_LIST_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"
WEATHERAPI_FORECAST_URL = "http://api.weatherapi.com/v1/forecast.json"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_EMOJI = {
    1000: "☀️",   # Sunny/Clear
    1003: "⛅",   # Partly cloudy
    1006: "☁️",   # Cloudy
    1009: "🌫️",  # Overcast
    1030: "🌫️",  # Mist
    1063: "🌦️",  # Patchy rain nearby
    1066: "🌨️",  # Patchy snow nearby
    1069: "🌨️",  # Patchy sleet nearby
    1072: "🌧️",  # Patchy freezing drizzle
    1087: "⛈️",  # Thundery outbreaks
    1114: "🌨️",  # Blowing snow
    1117: "❄️",  # Blizzard
    1135: "🌫️",  # Fog
    1147: "🌫️",  # Freezing fog
    1150: "🌧️",  # Patchy light drizzle
    1153: "🌧️",  # Light drizzle
    1168: "🌧️",  # Freezing drizzle
    1171: "🌧️",  # Heavy freezing drizzle
    1180: "🌧️",  # Patchy light rain
    1183: "🌧️",  # Light rain
    1186: "🌧️",  # Moderate rain at times
    1189: "🌧️",  # Moderate rain
    1192: "🌧️",  # Heavy rain at times
    1195: "🌧️",  # Heavy rain
    1198: "🌧️",  # Light freezing rain
    1201: "🌧️",  # Moderate or heavy freezing rain
    1204: "🌨️",  # Light sleet
    1207: "🌨️",  # Moderate or heavy sleet
    1210: "🌨️",  # Patchy light snow
    1213: "🌨️",  # Light snow
    1216: "🌨️",  # Patchy moderate snow
    1219: "🌨️",  # Moderate snow
    1222: "🌨️",  # Patchy heavy snow
    1225: "❄️",  # Heavy snow
    1237: "🧊",  # Ice pellets
    1240: "🌦️",  # Light rain shower
    1243: "🌧️",  # Moderate or heavy rain shower
    1246: "🌧️",  # Torrential rain shower
    1249: "🌨️",  # Light sleet showers
    1252: "🌨️",  # Moderate or heavy sleet showers
    1255: "🌨️",  # Light snow showers
    1258: "🌨️",  # Moderate or heavy snow showers
    1261: "🧊",  # Light showers of ice pellets
    1264: "🧊",  # Moderate or heavy showers of ice pellets
    1273: "⛈️",  # Patchy light rain with thunder
    1276: "⛈️",  # Moderate or heavy rain with thunder
    1279: "⛈️",  # Patchy light snow with thunder
    1282: "⛈️",  # Moderate or heavy snow with thunder
}

OPEN_METEO_WEATHER = {
    0: ("Sunny", "☀️"),
    1: ("Mainly Sunny", "🌤️"),
    2: ("Partly Cloudy", "⛅"),
    3: ("Cloudy", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Freezing Fog", "🌫️"),
    51: ("Light Drizzle", "🌧️"),
    53: ("Drizzle", "🌧️"),
    55: ("Heavy Drizzle", "🌧️"),
    56: ("Light Freezing Drizzle", "🌧️"),
    57: ("Freezing Drizzle", "🌧️"),
    61: ("Light Rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy Rain", "🌧️"),
    66: ("Light Freezing Rain", "🌧️"),
    67: ("Freezing Rain", "🌧️"),
    71: ("Light Snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy Snow", "❄️"),
    77: ("Snow Grains", "🌨️"),
    80: ("Light Rain Showers", "🌦️"),
    81: ("Rain Showers", "🌧️"),
    82: ("Heavy Rain Showers", "🌧️"),
    85: ("Snow Showers", "🌨️"),
    86: ("Heavy Snow Showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm With Hail", "⛈️"),
    99: ("Thunderstorm With Heavy Hail", "⛈️"),
}

EMAIL_SYSTEM_PROMPT = (
    "You are preparing a concise daily briefing in markdown for the user. "
    "Output ONLY the content for three sections. Do not mention Slack anywhere. "
    "Do not wrap output in code fences. Do not include a title or date header. "
    "All data is already provided in the JSON below — use only this data.\n\n"
    "Section 1: '## To-Do ✅' — actionable tasks derived from calendar and emails. "
    "Each item is a markdown checkbox: '- [ ] item'.\n\n"
    "Section 2: '## Calendar 📅' listing events as bullets (not checkboxes) "
    "with times in 12-hour format. The 'calendarDays' field tells you how many days "
    "of events are included. Group events by date with a bold date label "
    "(e.g. **Sunday 04/26**, **Monday 04/27**) when calendarDays > 1.\n\n"
    "If no calendar events exist, write exactly: 'No events scheduled.'\n\n"
    "Section 3: '## Email Highlights 📧' with a one-line summary header "
    "'**Starred:** N emails' followed by checklist items. List only the top 3 starred emails "
    "present in the data — do not add any extras. If no starred emails exist, "
    "write exactly: 'No starred emails.'\n\n"
    "If 'rolloverToDo' is present, include those unchecked items "
    "in the To-Do section — do not drop them. "
    "Never place rollover items in Calendar or Email Highlights.\n\n"
    "Do NOT generate a Hermes-to-do section. That section is written by the "
    "script directly from rollover data and is preserved across re-runs. "
    "Never invent Hermes-to-do content.\n\n"
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise RuntimeError(
                "Google OAuth refresh token expired or revoked. "
                "Re-authorize by running: python3 cli/google_reauth.py"
            ) from exc
        raise

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


def _fetch_json_url(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_coordinate_location(location: str) -> bool:
    return bool(re.match(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$", location.strip()))


def fetch_weather(api_key: str, location: str) -> tuple[dict | None, str | None]:
    """Fetch a 3-day forecast, preferring WeatherAPI and falling back to Open-Meteo."""
    params = urllib.parse.urlencode({
        "key": api_key,
        "q": location,
        "days": 3,
    })
    try:
        return _fetch_json_url(f"{WEATHERAPI_FORECAST_URL}?{params}"), "weatherapi"
    except Exception as exc:
        print(f"[WARN] WeatherAPI fetch failed: {exc}", file=sys.stderr)

    if not _is_coordinate_location(location):
        print("[WARN] Weather unavailable; Open-Meteo fallback requires lat,lon weather_location.", file=sys.stderr)
        return None, None

    latitude, longitude = [part.strip() for part in location.split(",", 1)]
    backup_params = urllib.parse.urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "timezone": LOCAL_TIMEZONE,
    })
    try:
        return _fetch_json_url(f"{OPEN_METEO_FORECAST_URL}?{backup_params}"), "open-meteo"
    except Exception as exc:
        print(f"[WARN] Open-Meteo weather fetch failed: {exc}", file=sys.stderr)
        return None, None


def _fahrenheit_to_celsius(value: float | int | None) -> int | None:
    if value is None:
        return None
    return round((float(value) - 32) * 5 / 9)


def _format_temp(value: float | int | None, include_celsius: bool = False) -> str:
    if value is None:
        return "N/A"
    fahrenheit = round(float(value))
    if include_celsius:
        celsius = _fahrenheit_to_celsius(fahrenheit)
        return f"{fahrenheit}°F ({celsius}°C)"
    return f"{fahrenheit}°F"


def _weather_day_label(date_text: str, index: int) -> str:
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d")
        weekday = day.strftime("%a" if index < 2 else "%A")
    except ValueError:
        weekday = date_text

    if index == 0:
        return f"Today ({weekday})"
    if index == 1:
        return f"Tomorrow ({weekday})"
    return weekday


def _format_weatherapi(data: dict) -> str:
    days = ((data.get("forecast") or {}).get("forecastday") or [])[:3]
    lines = ["## Weather ☀️"]

    for index, item in enumerate(days):
        date_text = item.get("date") or ""
        day = item.get("day") or {}
        condition = day.get("condition") or {}
        condition_text = condition.get("text") or "Forecast unavailable"
        condition_code = condition.get("code")
        emoji = WEATHER_EMOJI.get(condition_code, "🌡️")
        avg = day.get("avgtemp_f")
        high = day.get("maxtemp_f")
        low = day.get("mintemp_f")
        rain = day.get("daily_chance_of_rain", 0)
        label = _weather_day_label(date_text, index)
        lines.append(
            f"**{label}** — {emoji} {condition_text}, "
            f"{_format_temp(avg, include_celsius=True)} "
            f"↑{_format_temp(high)} ↓{_format_temp(low)}, {rain}% rain"
        )

    if len(lines) == 1:
        lines.append("*(Weather data unavailable)*")
    return "\n".join(lines) + "\n"


def _format_open_meteo(data: dict) -> str:
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_probability_max") or []
    lines = ["## Weather ☀️"]

    for index, date_text in enumerate(dates[:3]):
        code = codes[index] if index < len(codes) else None
        condition_text, emoji = OPEN_METEO_WEATHER.get(code, ("Forecast unavailable", "🌡️"))
        high = highs[index] if index < len(highs) else None
        low = lows[index] if index < len(lows) else None
        rain_chance = rain[index] if index < len(rain) else 0
        avg = (float(high) + float(low)) / 2 if high is not None and low is not None else None
        label = _weather_day_label(date_text, index)
        lines.append(
            f"**{label}** — {emoji} {condition_text}, "
            f"{_format_temp(avg, include_celsius=True)} "
            f"↑{_format_temp(high)} ↓{_format_temp(low)}, {rain_chance}% rain"
        )

    if len(lines) == 1:
        lines.append("*(Weather data unavailable)*")
    return "\n".join(lines) + "\n"


def format_weather(data: dict, source: str) -> str:
    if source == "open-meteo":
        body = _format_open_meteo(data)
    else:
        body = _format_weatherapi(data)
    return body + "[Hourly forecast →](https://www.wunderground.com/hourly/us/tx/mckinney)\n"


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


def read_text_with_retry(
    path: Path,
    attempts: int = 30,
    initial_delay: float = 0.5,
    max_delay: float = 4.0,
) -> str:
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last_exc = exc
            time.sleep(min(initial_delay * 2**i, max_delay))
    raise last_exc or OSError(f"Unable to read {path}")


def write_text_with_retry(
    path: Path,
    content: str,
    attempts: int = 30,
    initial_delay: float = 0.5,
    max_delay: float = 4.0,
) -> None:
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(min(initial_delay * 2**i, max_delay))
    raise last_exc or OSError(f"Unable to write {path}")


def get_yesterday_unchecked(date_str: str) -> tuple[list[str], list[str], list[str]]:
    """Extract unchecked items from To-Think, To-Do, and Hermes-to-do in yesterday's note."""
    if _HAS_ZONEINFO:
        tz = ZoneInfo(LOCAL_TIMEZONE)
        today = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        today = datetime.strptime(date_str, "%Y-%m-%d")
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_path = DAILY_NOTES_PATH / f"{yesterday}.md"

    if not yesterday_path.exists():
        return [], [], []

    content = read_text_with_retry(yesterday_path)
    lines = content.splitlines()

    current_section: str | None = None
    think_unchecked: list[str] = []
    todo_unchecked: list[str] = []
    hermes_unchecked: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "# To-Think 🧠":
            current_section = "think"
            continue
        if stripped == "## To-Do ✅":
            current_section = "todo"
            continue
        if stripped == HERMES_TODO_HEADER:
            current_section = "hermes"
            continue
        if stripped.startswith("# ") or stripped.startswith("## "):
            current_section = None
            continue
        if current_section and re.match(r"^- \[ \] .+$", line):
            if current_section == "think":
                think_unchecked.append(line)
            elif current_section == "todo":
                todo_unchecked.append(line)
            elif current_section == "hermes":
                hermes_unchecked.append(line)

    return think_unchecked, todo_unchecked, hermes_unchecked


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
            write_text_with_retry(out_path, existing)
        # Check for the main header or any partial briefing artifacts
        briefing_markers = (
            BRIEFING_HEADER,
            "#degraded-sync",
            "## Weather",
            "## Calendar 📅",
            "## Email Highlights 📧",
            "## Today's Focus 🧐",
            HERMES_TODO_HEADER,
        )
        if any(marker in existing for marker in briefing_markers):
            marker_positions = [existing.find(marker) for marker in briefing_markers if marker in existing]
            start = min(pos for pos in marker_positions if pos >= 0)
            updated = f"{existing[:start].rstrip()}\n\n{markdown}"
            write_text_with_retry(out_path, updated)
            print(f"[briefing_sync] replaced existing briefing content in {out_path.name}.")
            return out_path
        write_text_with_retry(out_path, existing + f"\n{markdown}")
    else:
        write_text_with_retry(out_path, f"{preamble}\n{markdown}")

    return out_path


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date to generate briefing for (YYYY-MM-DD). Defaults to today.")
    args = parser.parse_args()
    today = args.date if args.date else today_local()
    print(f"[briefing_sync] date={today}")
    print(f"[briefing_sync] output_path={DAILY_NOTES_PATH / f'{today}.md'}")

    # Ensure daily note exists even if OAuth or downstream API calls fail.
    out_path = DAILY_NOTES_PATH / f"{today}.md"
    if not out_path.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_with_retry(out_path, build_note_preamble(today))
        print(f"[briefing_sync] created stub note: {out_path.name}")

    # 1. Load credentials
    try:
        creds = load_credentials()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    # 2. Refresh Google OAuth2 access token
    try:
        access_token = refresh_access_token(creds)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Google OAuth token refresh failed: {exc}", file=sys.stderr)
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

    # 4. Fetch weather (optional and non-critical)
    weather_markdown = ""
    weather_key = creds.get("weatherapi_com_key", "")
    if weather_key:
        weather_location = creds.get("weather_location", "auto:ip")
        weather_data, weather_source = fetch_weather(weather_key, weather_location)
        if weather_data and weather_source:
            weather_markdown = format_weather(weather_data, weather_source)
            print(f"[briefing_sync] weather: fetched via {weather_source}")
        else:
            weather_markdown = "## Weather 🌡️ \n*(Weather data unavailable)*\n"

    # 5. Gather rollover items from yesterday
    rollover_failed = False
    try:
        think_rollover, todo_rollover, hermes_rollover = get_yesterday_unchecked(today)
    except OSError as exc:
        print(f"[ERROR] Could not read yesterday's note for rollover: {exc}", file=sys.stderr)
        rollover_failed = True
        think_rollover, todo_rollover, hermes_rollover = [], [], []
    if think_rollover or todo_rollover or hermes_rollover:
        print(
            "[briefing_sync] rollover: "
            f"{len(think_rollover)} To-Think, {len(todo_rollover)} To-Do, "
            f"{len(hermes_rollover)} Hermes-to-do unchecked item(s) from yesterday"
        )

    # 6. Generate briefing
    payload = {
        "date": today,
        "calendarDays": lookahead,
        "calendar": calendar_data,
        "starredEmails": email_data,
    }
    if todo_rollover:
        payload["rolloverToDo"] = todo_rollover

    minimax_failed = False
    try:
        ai_markdown = generate_briefing(payload, creds["minimax_api_key"])
    except Exception as exc:
        print(f"[ERROR] MiniMax generation failed: {exc}", file=sys.stderr)
        minimax_failed = True
        todo_lines = "\n".join(todo_rollover) + "\n" if todo_rollover else ""
        ai_markdown = (
            "## Calendar 📅\n*(AI summary unavailable — re-run to generate)*\n\n"
            "## Email Highlights 📧\n*(AI summary unavailable)*\n\n"
            "## Today's Focus 🧐\n*(AI summary unavailable)*\n\n"
            f"## To-Do ✅\n{todo_lines}"
        )

    # 7. Write to Obsidian Daily Notes
    if rollover_failed:
        think_section = "# To-Think 🧠\n*(Rollover from yesterday failed — re-run to retry)*\n"
    else:
        think_lines = "\n".join(think_rollover) + "\n" if think_rollover else ""
        think_section = f"# To-Think 🧠\n{think_lines}"

    # Hermes-to-do is always written by the script (never by the LLM) so the
    # section appears in the same place every day and rolls over reliably.
    hermes_lines = "\n".join(hermes_rollover) + "\n" if hermes_rollover else ""
    hermes_section = f"{HERMES_TODO_HEADER}\n{hermes_lines}"

    # Insert Hermes-to-do right after the LLM-generated "## To-Do ✅" section
    # so the layout is consistent: To-Think, To-Do, Hermes-to-do, Calendar, etc.
    ai_body = ai_markdown.strip()
    todo_marker = "## To-Do ✅"
    if todo_marker in ai_body:
        head, todo_block = ai_body.split(todo_marker, 1)
        # Reattach the marker (split() discards it) and find the next top-level
        # section boundary so we know where the To-Do block ends.
        lines = [todo_marker + "\n"] + todo_block.splitlines(keepends=True)
        rest_start = 0
        for idx, line in enumerate(lines[1:], start=1):
            if line.startswith("## "):
                rest_start = idx
                break
        else:
            rest_start = len(lines)
        todo_part = "".join(lines[:rest_start])
        rest_part = "".join(lines[rest_start:])
        # Collapse a leading blank line right after the header for clean output.
        todo_part = re.sub(r"\A(\s*## To-Do ✅\s*\n)\n+", r"\1", todo_part)
        ai_body = f"{head}{todo_part.rstrip()}\n\n{hermes_section.rstrip()}\n\n{rest_part.lstrip()}".rstrip()
    else:
        # Defensive fallback: LLM didn't emit To-Do (shouldn't happen); append.
        ai_body = f"{ai_body}\n\n{hermes_section}".rstrip()

    degraded_tag = "#degraded-sync\n\n" if (rollover_failed or minimax_failed) else ""
    markdown = f"{BRIEFING_HEADER}\n\n{degraded_tag}{weather_markdown}\n{think_section}\n{ai_body}\n"
    try:
        out_path = write_briefing(today, markdown)
        print(f"[briefing_sync] wrote to: {out_path}")
    except OSError as exc:
        print(f"[ERROR] Failed to write file: {exc}", file=sys.stderr)
        sys.exit(1)

    if minimax_failed:
        sys.exit(1)
    if rollover_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
