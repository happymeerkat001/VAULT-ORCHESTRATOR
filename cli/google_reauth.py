#!/usr/bin/env python3
"""Re-authorize Google OAuth and refresh token for vault-orchestrator.

Prerequisite:
  Add http://localhost:8085 to authorized redirect URIs in Google Cloud Console
  for your OAuth client.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


CREDENTIALS_PATH = Path("~/.config/vault-orchestrator/google_credentials").expanduser()
REDIRECT_URI = "http://localhost:8085"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDENTIALS_PATH}")
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        creds = json.load(f)
    required = ["google_client_id", "google_client_secret"]
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise ValueError(f"Missing credential keys: {', '.join(missing)}")
    return creds


def build_auth_url(client_id: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def capture_code_via_local_server(auth_url: str, expected_state: str) -> str:
    result: dict[str, str] = {}
    done = threading.Event()

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            if parsed.path != "/":
                self.send_response(404)
                self.end_headers()
                return

            if "error" in params:
                result["error"] = params.get("error", ["unknown_error"])[0]
            else:
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                if not code:
                    result["error"] = "missing_code"
                elif state != expected_state:
                    result["error"] = "invalid_state"
                else:
                    result["code"] = code

            ok = "code" in result
            body = (
                "Authorization complete. You can close this tab and return to terminal."
                if ok
                else f"Authorization failed: {result.get('error', 'unknown_error')}"
            )
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

            done.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("localhost", 8085), OAuthCallbackHandler)

    print("[reauth] Opening browser for Google authorization...")
    opened = webbrowser.open(auth_url)
    if not opened:
        print(f"[reauth] Browser did not open automatically. Visit:\n{auth_url}")

    server.serve_forever()
    done.wait(timeout=1)

    if "error" in result:
        raise RuntimeError(f"OAuth callback failed: {result['error']}")
    code = result.get("code")
    if not code:
        raise RuntimeError("No authorization code received")
    return code


def exchange_code_for_tokens(creds: dict, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": creds["google_client_id"],
            "client_secret": creds["google_client_secret"],
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def update_credentials_file(creds: dict, refresh_token: str) -> None:
    creds["google_refresh_token"] = refresh_token
    creds["google_redirect_uri"] = REDIRECT_URI
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")


def main() -> None:
    creds = load_credentials()
    state = secrets.token_urlsafe(16)
    auth_url = build_auth_url(creds["google_client_id"], state)
    code = capture_code_via_local_server(auth_url, state)
    token_data = exchange_code_for_tokens(creds, code)
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "No refresh_token returned. Ensure OAuth consent prompt is shown "
            "and this app requests offline access."
        )
    update_credentials_file(creds, refresh_token)
    print(f"[reauth] Updated credentials: {CREDENTIALS_PATH}")
    print("[reauth] Done. Run: python3 ingest/briefing_sync.py")


if __name__ == "__main__":
    main()
