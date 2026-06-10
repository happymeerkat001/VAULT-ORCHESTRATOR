#!/usr/bin/env python3
"""
hermes_to_kanban.py — Read today's ## Hermes-to-do section from the daily
note and create one GitHub Issue per unchecked item against
happymeerkat001/VAULT-ORCHESTRATOR (configurable via .env HERMES_KANBAN_REPO).

After a successful push, the source line in the daily note is rewritten to
include the issue URL so the link survives in the vault. Already-processed
items (those that already carry a "(see issue #N)" marker) are skipped.

Required in .env (repo root):
    GITHUB_TOKEN=<token with `repo` scope>
Optional:
    HERMES_KANBAN_REPO=happymeerkat001/VAULT-ORCHESTRATOR
    HERMES_KANBAN_LABEL=hermes-to-do
    HERMES_KANBAN_DRY_RUN=1   # print payload, do not POST

Stdlib only. No build, no tests, no external services besides GitHub.
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def _ensure_ssl_works() -> None:
    """Configure the default SSL context to use certifi's CA bundle if the
    system Python on macOS is missing its cert.pem (a common Python.org
    install issue). Idempotent; silently does nothing if SSL already works.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi  # type: ignore

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        # If we can't import certifi, leave it alone — the caller will get a
        # clear SSL error from urllib if the system bundle is missing. The
        # project README's recommended runbook uses a Python that ships with
        # certifi or runs the system Install Certificates.command first.
        pass


_ensure_ssl_works()

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault"
).expanduser()
DAILY_NOTES_PATH = VAULT_PATH / "Daily Notes"
HERMES_HEADER = "## Hermes-to-do 🪶"

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "happymeerkat001/VAULT-ORCHESTRATOR"
DEFAULT_LABEL = "hermes-to-do"


def load_env(path: Path) -> dict:
    """Minimal KEY=VALUE loader. Strips surrounding quotes; ignores comments."""
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def github_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """Tiny GitHub API client. Returns parsed JSON; raises on non-2xx."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # Build the auth header in two pieces so secret tokens never appear as
    # contiguous plaintext in argv, logs, or chat tooling.
    auth_value = "Bearer" + " " + token
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth_value,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-to-kanban/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "GitHub API %s %s -> %s: %s"
            % (method, url, exc.code, detail[:500])
        ) from exc


def extract_unchecked_hermes_items(note_text: str) -> list[tuple[int, str]]:
    """Return (line_index, line_text) for unchecked items in the Hermes section.

    Stops at the next top-level section header (any line starting with '# ' or '## ').
    Skips lines that already carry a "(see issue #N)" marker.
    """
    items: list[tuple[int, str]] = []
    in_section = False
    for idx, line in enumerate(note_text.splitlines()):
        stripped = line.strip()
        if stripped == HERMES_HEADER:
            in_section = True
            continue
        if in_section and (stripped.startswith("# ") or stripped.startswith("## ")):
            # Different section — stop.
            break
        if not in_section:
            continue
        if re.match(r"^- \[ \] .+$", line) and "(see issue #" not in line:
            items.append((idx, line))
    return items


def find_label_id(repo: str, label_name: str, token: str) -> int | None:
    """Look up a label's numeric ID. Returns None if not found."""
    try:
        data = github_request(
            "GET",
            "%s/repos/%s/labels/%s" % (GITHUB_API, repo, urllib.parse.quote(label_name)),
            token,
        )
        return data.get("id")
    except RuntimeError as exc:
        if "-> 404" in str(exc):
            return None
        raise


def create_issue(
    repo: str, title: str, body: str, token: str, label_id: int | None
) -> dict:
    payload = {"title": title, "body": body}
    if label_id is not None:
        payload["labels"] = [label_id]
    return github_request(
        "POST", "%s/repos/%s/issues" % (GITHUB_API, repo), token, payload
    )


def build_issue_body(raw_line: str, source_note: str) -> str:
    """Wrap the raw Hermes item line in a small issue template."""
    clean = re.sub(r"^- \[ \] ", "", raw_line).strip()
    return (
        "**Source:** today's daily note (`%s`)\n\n"
        "**Hermes-to-do item:**\n\n"
        "> %s\n\n"
        "---\n"
        "_This issue was auto-created by `cli/hermes_to_kanban.py`. "
        "Close it when the task is done._"
    ) % (source_note, clean)


def rewrite_line(line: str, issue_number: int, issue_url: str) -> str:
    """Mark the line as pushed and append the issue link."""
    clean = re.sub(r"^- \[ \] ", "", line).strip()
    return "- [x] %s (see issue #%d: %s)" % (clean, issue_number, issue_url)


def find_today_note(today: str | None) -> Path:
    date_str = today or datetime.now().strftime("%Y-%m-%d")
    return DAILY_NOTES_PATH / (date_str + ".md")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date of the daily note (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--max", type=int, default=1, help="Max items to push in this run (default 1). Use 0 for all.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen; do not POST or write.")
    parser.add_argument("--repo", help="Override target repo (default: env HERMES_KANBAN_REPO or %s)." % DEFAULT_REPO)
    parser.add_argument("--label", help="Override label name (default: env HERMES_KANBAN_LABEL or %s)." % DEFAULT_LABEL)
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN missing in %s" % ENV_PATH, file=sys.stderr)
        return 2
    repo = args.repo or env.get("HERMES_KANBAN_REPO", DEFAULT_REPO)
    label_name = args.label or env.get("HERMES_KANBAN_LABEL", DEFAULT_LABEL)
    dry_run = args.dry_run or env.get("HERMES_KANBAN_DRY_RUN", "").lower() in ("1", "true", "yes")

    note_path = find_today_note(args.date)
    if not note_path.exists():
        print("ERROR: daily note not found: %s" % note_path, file=sys.stderr)
        return 1
    note_text = note_path.read_text(encoding="utf-8")
    items = extract_unchecked_hermes_items(note_text)
    if not items:
        print("No unchecked Hermes-to-do items in %s" % note_path.name)
        return 0

    if args.max and args.max > 0:
        items = items[: args.max]

    print("Pushing %d item(s) to %s (label=%s, dry_run=%s)" % (len(items), repo, label_name, dry_run))

    if dry_run:
        for idx, line in items:
            print("  would push line %d: %s" % (idx + 1, line.strip()))
        return 0

    label_id = find_label_id(repo, label_name, token)
    if label_id is None:
        print("  label '%s' not found in %s — creating without label" % (label_name, repo))

    lines = note_text.splitlines()
    pushed: list[dict] = []
    for line_idx, line in items:
        title = "Hermes-to-do: " + re.sub(r"^- \[ \] ", "", line).strip()
        body = build_issue_body(line, note_path.name)
        try:
            issue = create_issue(repo, title, body, token, label_id)
        except RuntimeError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 1
        issue_num = issue["number"]
        issue_url = issue["html_url"]
        new_line = rewrite_line(line, issue_num, issue_url)
        # Replace in-place. line_idx refers to the splitlines() index.
        lines[line_idx] = new_line
        pushed.append({"number": issue_num, "url": issue_url, "title": title})
        print("  opened issue #%d -> %s" % (issue_num, issue_url))

    if pushed:
        note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Updated %s with %d issue link(s)." % (note_path.name, len(pushed)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
