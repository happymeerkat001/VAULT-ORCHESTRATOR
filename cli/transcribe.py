#!/usr/bin/env python3
"""
transcribe.py - Submit a media URL to Transcript.lol and print the transcript.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/transcribe.py \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

Auth test:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/transcribe.py --test-auth

Optional env vars in .env or process environment:
  FIREBASE_API_KEY
  TRANSCRIPT_LOL_SPACE_ID
  TRANSCRIPT_LOL_SPACE_NAME
  TRANSCRIPT_LOL_API_KEY
  TRANSCRIPT_LOL_AUTH_TOKEN
  TRANSCRIPT_LOL_SESSION_COOKIE
  Transcript.lol_Login
  Transcript.lol_Password

No external dependencies - stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from media_captions import fetch_vimeo_captions

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

BASE_URL = "https://transcript.lol"
API_BASE_URL = f"{BASE_URL}/api/v1"
FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
DEFAULT_SPACE_ID = "678568d76d74d77ee0ef382c"
DEFAULT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SOURCE_MAP = {
    "youtube.com": "YOUTUBE",
    "youtu.be": "YOUTUBE",
    "vimeo.com": "VIMEO",
    "instagram.com": "INSTAGRAM",
    "x.com": "X",
    "twitter.com": "X",
    "facebook.com": "FACEBOOK",
}

VIDEO_SOURCES = {"YOUTUBE", "VIMEO", "INSTAGRAM", "X", "FACEBOOK"}
TERMINAL_STATUSES = {"COMPLETED", "COMPLETE", "DONE", "READY", "SUCCEEDED", "SUCCESS", "TRANSCRIPTION_COMPLETE"}
FAILED_STATUSES = {"FAILED", "ERROR", "CANCELLED", "REJECTED"}


def _ensure_ssl_certs() -> None:
    """Set SSL_CERT_FILE from certifi if the default cert bundle is missing."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    import ssl
    default_cafile = ssl.get_default_verify_paths().cafile
    if default_cafile and os.path.isfile(default_cafile):
        return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def load_env(env_path: Path | None = None) -> dict[str, str]:
    _ensure_ssl_certs()
    values: dict[str, str] = {}
    path = env_path or ENV_PATH
    if path.exists():
        pattern = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"?([^"]*)"?\s*$')
        for line in path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line)
            if match:
                values[match.group(1)] = match.group(2)

    for key, value in os.environ.items():
        if key in values or key.startswith('TRANSCRIPT_LOL_') or key.startswith('FIREBASE_') or key.startswith('SKOOL_') or key == 'Transcript.lol_Login' or key == 'Transcript.lol_Password':
            values[key] = value
    return values


def extract_youtube_id(url: str) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/")

    if host in {"youtube.com", "m.youtube.com"}:
        if path == "watch":
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            return video_id or None
        if path.startswith("shorts/") or path.startswith("embed/") or path.startswith("live/"):
            parts = path.split("/")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    if host == "youtu.be":
        parts = path.split("/")
        if parts and parts[0]:
            return parts[0]
    return None


def normalize_recordings_payload(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("recordings", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            return []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def urls_match(source_url: str, target_url: str) -> bool:
    source_video_id = extract_youtube_id(source_url)
    target_video_id = extract_youtube_id(target_url)
    if source_video_id and target_video_id:
        return source_video_id == target_video_id

    source_parts = urllib.parse.urlsplit((source_url or "").strip())
    target_parts = urllib.parse.urlsplit((target_url or "").strip())
    normalized_source = urllib.parse.urlunsplit(
        (
            source_parts.scheme.lower(),
            source_parts.netloc.lower(),
            source_parts.path.rstrip("/"),
            source_parts.query,
            "",
        )
    )
    normalized_target = urllib.parse.urlunsplit(
        (
            target_parts.scheme.lower(),
            target_parts.netloc.lower(),
            target_parts.path.rstrip("/"),
            target_parts.query,
            "",
        )
    )
    return normalized_source == normalized_target


class TranscriptClient:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.space_name = (env.get("TRANSCRIPT_LOL_SPACE_NAME") or "").strip()
        self.space_id = (env.get("TRANSCRIPT_LOL_SPACE_ID") or DEFAULT_SPACE_ID).strip() or DEFAULT_SPACE_ID
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.auth_headers: dict[str, str] = {}
        self.firebase_tokens: dict[str, str] = {}

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        accept_json: bool = True,
    ) -> tuple[int, bytes, dict[str, str]]:
        merged_headers = {
            "Accept": "application/json" if accept_json else "*/*",
            "User-Agent": _UA,
            **self.auth_headers,
        }
        if headers:
            merged_headers.update(headers)

        req = urllib.request.Request(url, data=data, method=method, headers=merged_headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                body = resp.read()
                return resp.getcode(), body, dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers.items())

    def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list:
        body = None
        req_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        status, raw, _ = self._request(url, method=method, data=body, headers=req_headers)
        if status >= 400:
            text = raw.decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {status} from {url}: {text[:500]}")
        return json.loads(raw.decode("utf-8"))

    def authenticate(self) -> None:
        api_key = self.env.get("TRANSCRIPT_LOL_API_KEY")
        if api_key:
            self.auth_headers = {"x-api-key": api_key}
            self._verify_auth("api key")
            self._resolve_space_id()
            return

        auth_token = self.env.get("TRANSCRIPT_LOL_AUTH_TOKEN")
        if auth_token:
            self.auth_headers = {"Authorization": auth_token}
            self._verify_auth("auth token")
            self._resolve_space_id()
            return

        session_cookie = self.env.get("TRANSCRIPT_LOL_SESSION_COOKIE")
        if session_cookie:
            self.auth_headers = {"Cookie": session_cookie}
            self._verify_auth("session cookie")
            self._resolve_space_id()
            return

        email = self.env.get("Transcript.lol_Login")
        password = self.env.get("Transcript.lol_Password")
        if not email or not password:
            raise RuntimeError(
                "No Transcript.lol auth found. Set one of: TRANSCRIPT_LOL_API_KEY, "
                "TRANSCRIPT_LOL_AUTH_TOKEN, TRANSCRIPT_LOL_SESSION_COOKIE, or "
                "Transcript.lol_Login + Transcript.lol_Password."
            )

        firebase_api_key = self.env.get("FIREBASE_API_KEY", "")
        if firebase_api_key:
            self._login_with_firebase(email, password)
            self._establish_transcript_auth()
            self._resolve_space_id()
            return

        self._login_with_browser_session(email, password)
        self._verify_auth("browser session")
        self._resolve_space_id()

    def _resolve_space_id(self) -> None:
        if not self.space_name:
            return
        data = self._json_request(f"{API_BASE_URL}/spaces")
        spaces = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        for space in spaces:
            name = space.get("name")
            sid = space.get("id") or space.get("_id")
            if isinstance(name, str) and name.strip() == self.space_name and isinstance(sid, str) and sid.strip():
                self.space_id = sid.strip()
                return
        raise RuntimeError(f"Workspace named {self.space_name!r} was not found in Transcript.lol spaces list.")

    def _login_with_browser_session(self, email: str, password: str) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for browser-session Transcript.lol login when FIREBASE_API_KEY is unavailable. "
                "Install it with: python3 -m pip install playwright && python3 -m playwright install chromium"
            ) from exc

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{BASE_URL}/auth/login", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
            if page.locator('input[type="email"]').count() > 0:
                page.locator('input[type="email"]').first.fill(email)
                page.locator('input[type="email"]').first.press('Enter')
                page.wait_for_timeout(1200)
            if page.locator('input[type="password"]').count() == 0:
                raise RuntimeError("Transcript.lol login page did not expose a password input during browser auth.")
            page.locator('input[type="password"]').first.fill(password)
            page.locator('button:has-text("Sign In")').first.click()
            page.wait_for_timeout(6000)
            cookies = page.context.cookies()
            browser.close()

        if not cookies:
            raise RuntimeError("Transcript.lol browser login did not produce any cookies.")
        self._load_cookies(cookies)

    def _load_cookies(self, cookies: list[dict[str, object]]) -> None:
        for cookie in cookies:
            name = str(cookie.get('name') or '').strip()
            value = str(cookie.get('value') or '')
            domain = str(cookie.get('domain') or '').strip()
            if not name or not domain:
                continue
            path = str(cookie.get('path') or '/') or '/'
            secure = bool(cookie.get('secure', False))
            expires = cookie.get('expires')
            expires_int = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
            self.cookie_jar.set_cookie(Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith('.'),
                path=path,
                path_specified=True,
                secure=secure,
                expires=expires_int,
                discard=False,
                comment=None,
                comment_url=None,
                rest={'HttpOnly': 'True' if cookie.get('httpOnly', False) else 'False'},
                rfc2109=False,
            ))

    def _verify_auth(self, auth_mode: str) -> None:
        for candidate in (
            f"{API_BASE_URL}/me",
            f"{API_BASE_URL}/spaces/{self.space_id}",
            f"{API_BASE_URL}/spaces/{self.space_id}/recordings",
        ):
            status, raw, _ = self._request(candidate)
            if status < 400:
                print(f"[transcribe] authenticated via {auth_mode}")
                return
            if status not in (401, 403, 404):
                text = raw.decode("utf-8", errors="replace")
                raise RuntimeError(f"Auth probe failed at {candidate}: HTTP {status}: {text[:300]}")
        raise RuntimeError(f"Unable to verify Transcript.lol auth via {auth_mode}.")

    def _login_with_firebase(self, email: str, password: str) -> None:
        firebase_api_key = self.env.get("FIREBASE_API_KEY", "")
        if not firebase_api_key:
            raise RuntimeError("FIREBASE_API_KEY not set in .env")
        url = f"{FIREBASE_SIGN_IN_URL}?key={firebase_api_key}"
        data = self._json_request(
            url,
            method="POST",
            payload={
                "email": email,
                "password": password,
                "returnSecureToken": True,
            },
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Firebase auth response: {json.dumps(data)[:500]}")

        id_token = data.get("idToken")
        refresh_token = data.get("refreshToken")
        if not id_token or not refresh_token:
            raise RuntimeError(f"Firebase auth response missing tokens: {json.dumps(data)[:500]}")

        self.firebase_tokens = {
            "idToken": str(id_token),
            "refreshToken": str(refresh_token),
        }
        print("[transcribe] Firebase sign-in succeeded")

    def _establish_transcript_auth(self) -> None:
        id_token = self.firebase_tokens["idToken"]
        refresh_token = self.firebase_tokens["refreshToken"]

        exchange_attempts = [
            (
                f"{BASE_URL}/api/auth/session",
                {"id_token": id_token, "refresh_token": refresh_token},
            ),
            (
                f"{BASE_URL}/api/auth/firebase",
                {"idToken": id_token, "refreshToken": refresh_token},
            ),
            (
                f"{BASE_URL}/auth/session",
                {"id_token": id_token, "refresh_token": refresh_token},
            ),
            (
                f"{BASE_URL}/api/v1/auth/session",
                {"id_token": id_token, "refresh_token": refresh_token},
            ),
        ]

        for url, payload in exchange_attempts:
            status, raw, _ = self._request(
                url,
                method="POST",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/auth/login",
                },
            )
            if status < 400 and self._has_auth_token_cookie():
                self.auth_headers = {}
                self._verify_auth(f"firebase exchange at {url}")
                return
            if status not in (401, 403, 404):
                text = raw.decode("utf-8", errors="replace")
                print(
                    f"[transcribe] auth exchange probe at {url} -> HTTP {status}: {text[:200]}",
                    file=sys.stderr,
                )

        unsigned_auth_token = self._build_unsigned_auth_token(id_token, refresh_token)
        strategies = [
            ("firebase bearer token", {"Authorization": f"Bearer {id_token}"}),
            ("firebase id token", {"Authorization": id_token}),
            ("AuthToken cookie", {"Cookie": f"AuthToken={unsigned_auth_token}"}),
        ]
        for auth_mode, headers in strategies:
            self.auth_headers = headers
            try:
                self._verify_auth(auth_mode)
                return
            except RuntimeError as exc:
                print(f"[transcribe] auth strategy failed: {auth_mode}: {exc}", file=sys.stderr)

        raise RuntimeError(
            "Firebase sign-in worked, but Transcript.lol API auth was rejected. "
            "If the site requires a signed AuthToken cookie exchange, add a working "
            "TRANSCRIPT_LOL_AUTH_TOKEN or TRANSCRIPT_LOL_SESSION_COOKIE to .env."
        )

    def _has_auth_token_cookie(self) -> bool:
        return any(cookie.name == "AuthToken" for cookie in self.cookie_jar)

    @staticmethod
    def _build_unsigned_auth_token(id_token: str, refresh_token: str) -> str:
        header = {"alg": "none", "typ": "JWT"}
        payload = {"id_token": id_token, "refresh_token": refresh_token}

        def encode_segment(value: dict[str, str]) -> str:
            raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{encode_segment(header)}.{encode_segment(payload)}."

    def create_recording(
        self,
        *,
        url: str,
        title: str,
        language: str,
        media_type: str,
        source: str,
        external_id: str | None = None,
    ) -> str:
        payload = {
            "title": title,
            "language": language,
            "mediaType": media_type,
            "source": source,
            "sourceUrl": url,
        }
        if external_id and external_id.strip():
            payload["externalId"] = external_id.strip()
        data = self._json_request(
            f"{API_BASE_URL}/spaces/{self.space_id}/recordings",
            method="POST",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected create response: {json.dumps(data)[:500]}")
        recording_id = data.get("id") or data.get("recordingId")
        if not recording_id:
            raise RuntimeError(f"Create response missing recording id: {json.dumps(data)[:500]}")
        return str(recording_id)

    def get_recording(self, recording_id: str) -> dict:
        data = self._json_request(f"{API_BASE_URL}/spaces/{self.space_id}/recordings/{recording_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected recording response: {json.dumps(data)[:500]}")
        return data

    def find_recording_by_url(self, url: str) -> str | None:
        cleaned_url = (url or "").strip()
        if not cleaned_url:
            return None

        payload = self._json_request(f"{API_BASE_URL}/spaces/{self.space_id}/recordings")
        for recording in normalize_recordings_payload(payload):
            source_url = recording.get("sourceUrl", "")
            recording_id = recording.get("id") or recording.get("recordingId")
            if not isinstance(source_url, str) or not recording_id:
                continue
            if not urls_match(source_url, cleaned_url):
                continue
            status = extract_status(recording)
            if status in TERMINAL_STATUSES or status in FAILED_STATUSES or status.endswith("_FAILED"):
                return str(recording_id)
        return None

    def list_insights(self, recording_id: str) -> dict | list:
        return self._json_request(
            f"{API_BASE_URL}/spaces/{self.space_id}/recordings/{recording_id}/insights"
        )

    def create_insight(
        self,
        recording_id: str,
        prompt_id: str,
        tweak_query: str = "",
    ) -> dict | list:
        payload: dict[str, str] = {"promptId": prompt_id}
        if tweak_query.strip():
            payload["tweakQuery"] = tweak_query.strip()
        return self._json_request(
            f"{API_BASE_URL}/spaces/{self.space_id}/recordings/{recording_id}/insights",
            method="POST",
            payload=payload,
        )

    def get_transcript(self, recording_id: str, fmt: str) -> str:
        status, raw, headers = self._request(
            f"{API_BASE_URL}/spaces/{self.space_id}/recordings/{recording_id}/transcript?format={urllib.parse.quote(fmt)}",
            accept_json=(fmt == "json"),
        )
        if status >= 400:
            text = raw.decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {status} from transcript endpoint: {text[:500]}")
        content_type = headers.get("Content-Type", "")
        if "application/json" in content_type or fmt == "json":
            return json.dumps(json.loads(raw.decode("utf-8")), indent=2)
        return raw.decode("utf-8", errors="replace")


def detect_source(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for domain, source in SOURCE_MAP.items():
        if host == domain or host.endswith(f".{domain}"):
            return source
    return "UNKNOWN"


def detect_media_type(source: str) -> str:
    return "VIDEO" if source in VIDEO_SOURCES else "AUDIO"


def derive_title(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.path and parsed.path != "/":
        return f"{host}{parsed.path}"
    return host or url


def extract_status(recording: dict) -> str:
    for key in ("status", "state", "processingStatus"):
        value = recording.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    transcript = recording.get("transcript")
    if isinstance(transcript, dict):
        for key in ("status", "state", "processingStatus"):
            value = transcript.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return "UNKNOWN"


def wait_for_recording_terminal(
    client: TranscriptClient,
    recording_id: str,
    timeout_seconds: int,
) -> dict:
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        recording = client.get_recording(recording_id)
        status = extract_status(recording)
        if status != last_status:
            print(f"[transcribe] status={status}", flush=True)
            last_status = status

        if status in FAILED_STATUSES or status.endswith("_FAILED"):
            raise RuntimeError(f"Transcript.lol marked recording as failed: {json.dumps(recording)[:500]}")

        if status in TERMINAL_STATUSES:
            return recording

        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"Timed out after {timeout_seconds}s waiting for recording.")


def wait_for_transcript(client: TranscriptClient, recording_id: str, fmt: str, timeout_seconds: int) -> str:
    wait_for_recording_terminal(client, recording_id, timeout_seconds)
    return client.get_transcript(recording_id, fmt)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a URL to Transcript.lol and print the transcript.")
    parser.add_argument("url", nargs="?", help="Media URL to transcribe")
    parser.add_argument("--language", default="en", help="Transcript language (default: en)")
    parser.add_argument(
        "--format",
        default="text",
        choices=["json", "text", "csv", "srt", "vtt", "pdf", "word"],
        help="Transcript output format (default: text)",
    )
    parser.add_argument("--title", help="Optional title override")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Polling timeout in seconds")
    parser.add_argument("--test-auth", action="store_true", help="Authenticate and exit without creating a recording")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_env()
    if args.test_auth:
        client = TranscriptClient(env)
        client.authenticate()
        print(f"[transcribe] auth ok for space_id={client.space_id}")
        return

    if not args.url:
        raise RuntimeError("URL is required unless --test-auth is used.")

    source = detect_source(args.url)
    media_type = detect_media_type(source)
    title = args.title or derive_title(args.url)

    print(f"[transcribe] source={source} media_type={media_type} language={args.language}")

    if source == "VIMEO":
        transcript = fetch_vimeo_captions(args.url, args.language)
        if transcript:
            sys.stdout.write(transcript)
            if not transcript.endswith("\n"):
                sys.stdout.write("\n")
            return
        print("[transcribe] no Vimeo captions found; falling back to Transcript.lol")

    client = TranscriptClient(env)
    client.authenticate()
    try:
        recording_id = client.find_recording_by_url(args.url)
        if recording_id:
            print(f"[transcribe] reusing existing recording {recording_id}")
        else:
            recording_id = client.create_recording(
                url=args.url,
                title=title,
                language=args.language,
                media_type=media_type,
                source=source,
            )
            print(f"[transcribe] recording_id={recording_id}")

        transcript = wait_for_transcript(client, recording_id, args.format, args.timeout)
    except Exception as exc:
        if source == "VIMEO":
            raise RuntimeError(
                f"No Vimeo captions found; Transcript.lol media import failed. {exc}"
            ) from exc
        raise
    sys.stdout.write(transcript)
    if transcript and not transcript.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[transcribe] interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
