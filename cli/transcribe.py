#!/usr/bin/env python3
"""
transcribe.py - Submit a media URL to Transcript.lol and print the transcript.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/transcribe.py \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

Optional env vars in .env:
  TRANSCRIPT_LOL_SPACE_ID
  TRANSCRIPT_LOL_API_KEY
  TRANSCRIPT_LOL_AUTH_TOKEN
  TRANSCRIPT_LOL_SESSION_COOKIE
  Transcript.lol_Login
  Transcript.lol_Password

No external dependencies - stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

BASE_URL = "https://transcript.lol"
API_BASE_URL = f"{BASE_URL}/api/v1"
DEFAULT_SPACE_ID = "69c31598e83d93ed1074a9e8"
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
TERMINAL_STATUSES = {"COMPLETED", "COMPLETE", "DONE", "READY", "SUCCEEDED", "SUCCESS"}
FAILED_STATUSES = {"FAILED", "ERROR", "CANCELLED", "REJECTED"}


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values

    pattern = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"?([^"]*)"?\s*$')
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


class TranscriptClient:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.space_id = env.get("TRANSCRIPT_LOL_SPACE_ID", DEFAULT_SPACE_ID)
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.auth_headers: dict[str, str] = {}

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
            return

        auth_token = self.env.get("TRANSCRIPT_LOL_AUTH_TOKEN")
        if auth_token:
            self.auth_headers = {"Authorization": auth_token}
            self._verify_auth("auth token")
            return

        session_cookie = self.env.get("TRANSCRIPT_LOL_SESSION_COOKIE")
        if session_cookie:
            self.auth_headers = {"Cookie": session_cookie}
            self._verify_auth("session cookie")
            return

        email = self.env.get("Transcript.lol_Login")
        password = self.env.get("Transcript.lol_Password")
        if not email or not password:
            raise RuntimeError(
                "No Transcript.lol auth found. Set one of: TRANSCRIPT_LOL_API_KEY, "
                "TRANSCRIPT_LOL_AUTH_TOKEN, TRANSCRIPT_LOL_SESSION_COOKIE, or "
                "Transcript.lol_Login + Transcript.lol_Password."
            )

        self._login_with_credentials(email, password)
        self._verify_auth("email/password login")

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

    def _login_with_credentials(self, email: str, password: str) -> None:
        attempts = [
            {
                "url": f"{BASE_URL}/auth/login",
                "payload": {"email": email, "password": password},
                "content_type": "application/json",
            },
            {
                "url": f"{BASE_URL}/auth/login",
                "payload": {"email": email, "password": password},
                "content_type": "application/x-www-form-urlencoded",
            },
            {
                "url": f"{BASE_URL}/api/auth/login",
                "payload": {"email": email, "password": password},
                "content_type": "application/json",
            },
            {
                "url": f"{BASE_URL}/api/v1/auth/login",
                "payload": {"email": email, "password": password},
                "content_type": "application/json",
            },
        ]

        for attempt in attempts:
            payload = attempt["payload"]
            if attempt["content_type"] == "application/json":
                data = json.dumps(payload).encode("utf-8")
            else:
                data = urllib.parse.urlencode(payload).encode("utf-8")
            status, raw, headers = self._request(
                attempt["url"],
                method="POST",
                data=data,
                headers={
                    "Content-Type": attempt["content_type"],
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/auth/login",
                },
            )
            auth_cookie_names = self._auth_cookie_names()
            location = headers.get("Location", "")
            if status < 400 and (auth_cookie_names or self._looks_like_authenticated_redirect(location)):
                print(f"[transcribe] login accepted at {attempt['url']}")
                return
            if status in (301, 302, 303, 307, 308) and (
                auth_cookie_names or self._looks_like_authenticated_redirect(location)
            ):
                print(f"[transcribe] login redirected from {attempt['url']}")
                return
            print(f"[transcribe] login attempt failed: {attempt['url']} -> HTTP {status}", file=sys.stderr)

        raise RuntimeError(
            "Transcript.lol credential login failed on all known endpoints. "
            "The public /auth/login route only returned non-auth cookies during verification. "
            "Set TRANSCRIPT_LOL_API_KEY, TRANSCRIPT_LOL_AUTH_TOKEN, or "
            "TRANSCRIPT_LOL_SESSION_COOKIE to bypass web login discovery."
        )

    def _auth_cookie_names(self) -> list[str]:
        ignored = {"lb_affinity", "NEXT_LOCALE"}
        names = []
        for cookie in self.cookie_jar:
            if cookie.name not in ignored:
                names.append(cookie.name)
        return names

    @staticmethod
    def _looks_like_authenticated_redirect(location: str) -> bool:
        if not location:
            return False
        lowered = location.lower()
        if "/auth/login" in lowered or "/login" == lowered.rstrip("/"):
            return False
        return "/dashboard" in lowered or "/spaces/" in lowered or "/recordings" in lowered

    def create_recording(
        self,
        *,
        url: str,
        title: str,
        language: str,
        media_type: str,
        source: str,
    ) -> str:
        payload = {
            "title": title,
            "language": language,
            "mediaType": media_type,
            "source": source,
            "sourceUrl": url,
        }
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
    return "UNKNOWN"


def wait_for_transcript(client: TranscriptClient, recording_id: str, fmt: str, timeout_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        recording = client.get_recording(recording_id)
        status = extract_status(recording)
        if status != last_status:
            print(f"[transcribe] status={status}")
            last_status = status

        if status in FAILED_STATUSES:
            raise RuntimeError(f"Transcript.lol marked recording as failed: {json.dumps(recording)[:500]}")

        if status in TERMINAL_STATUSES:
            return client.get_transcript(recording_id, fmt)

        try:
            return client.get_transcript(recording_id, fmt)
        except RuntimeError:
            time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"Timed out after {timeout_seconds}s waiting for transcript.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a URL to Transcript.lol and print the transcript.")
    parser.add_argument("url", help="Media URL to transcribe")
    parser.add_argument("--language", default="en", help="Transcript language (default: en)")
    parser.add_argument(
        "--format",
        default="text",
        choices=["json", "text", "csv", "srt", "vtt", "pdf", "word"],
        help="Transcript output format (default: text)",
    )
    parser.add_argument("--title", help="Optional title override")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Polling timeout in seconds")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_env()

    source = detect_source(args.url)
    media_type = detect_media_type(source)
    title = args.title or derive_title(args.url)

    print(f"[transcribe] source={source} media_type={media_type} language={args.language}")

    client = TranscriptClient(env)
    client.authenticate()

    recording_id = client.create_recording(
        url=args.url,
        title=title,
        language=args.language,
        media_type=media_type,
        source=source,
    )
    print(f"[transcribe] recording_id={recording_id}")

    transcript = wait_for_transcript(client, recording_id, args.format, args.timeout)
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
