#!/usr/bin/env python3
"""
hermes_kanban_server.py — Tiny stdlib HTTP server that exposes the same
shape the user originally tried to hit at 127.0.0.1:9119/kanban, but on
port 9120 (9119 is the Hermes dashboard and is taken).

Endpoints:
    GET  /health        -> {"ok": true}
    POST /kanban        -> runs cli/hermes_to_kanban.py with --max 1
    POST /kanban/all    -> runs with --max 0 (pushes every unchecked item)
    POST /kanban/dry    -> runs with --dry-run (no writes)

The server never imports the kanban module directly. It shells out so a
crash in the worker can't take down the listener, and the caller's stderr
flows back to the HTTP response for easy debugging.

Stdlib only. Run with: python3 cli/hermes_kanban_server.py
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "cli" / "hermes_to_kanban.py"
DEFAULT_PORT = 9120


def _venv_python() -> str:
    """Prefer the Hermes venv python so SSL works on macOS Python installs."""
    venv = Path("/Users/leon/.hermes/hermes-agent/venv/bin/python3")
    return str(venv) if venv.exists() else sys.executable


def _run_kanban(args: list[str]) -> tuple[int, str, str]:
    env = os.environ.copy()
    proc = subprocess.run(
        [_venv_python(), str(SCRIPT), *args],
        capture_output=True,
        timeout=60,
        env=env,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._json(200, {"ok": True, "script": str(SCRIPT), "port": DEFAULT_PORT})
            return
        self._json(404, {"ok": False, "error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/kanban":
            code, out, err = _run_kanban(["--max", "1"])
        elif self.path == "/kanban/all":
            code, out, err = _run_kanban(["--max", "0"])
        elif self.path == "/kanban/dry":
            code, out, err = _run_kanban(["--dry-run"])
        else:
            self._json(404, {"ok": False, "error": "not found", "path": self.path})
            return
        self._json(200 if code == 0 else 500, {
            "ok": code == 0,
            "exit_code": code,
            "stdout": out,
            "stderr": err,
        })

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Route access logs to stderr but keep them concise.
        sys.stderr.write("[hermes-kanban] " + (format % args) + "\n")


def main() -> int:
    port = int(os.environ.get("HERMES_KANBAN_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("hermes-kanban listening on http://127.0.0.1:%d" % port)
    print("endpoints: GET  /health  |  POST /kanban  |  /kanban/all  |  /kanban/dry")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
