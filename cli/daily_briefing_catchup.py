#!/usr/bin/env python3
"""Detect and repair missing Daily Note morning briefings."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingest.daily_note_helpers import ensure_daily_note_preamble, read_text_with_retry

BRIEFING_HEADER = "## Morning Briefing ☀️"
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault"
).expanduser()
DAILY_NOTES_PATH = VAULT_PATH / "Daily Notes"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run briefing_sync only when a Daily Note is missing its Morning Briefing."
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Daily note date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run briefing_sync even if the Morning Briefing marker is already present.",
    )
    return parser.parse_args()


def has_morning_briefing(note_path: Path) -> bool:
    return note_path.exists() and BRIEFING_HEADER in read_text_with_retry(note_path)


def run_briefing_sync(date_str: str) -> int:
    cmd = [sys.executable, str(REPO_ROOT / "ingest" / "briefing_sync.py"), "--date", date_str]
    return subprocess.run(cmd, cwd=REPO_ROOT, check=False).returncode


def catch_up(date_str: str, force: bool = False) -> int:
    note_path = ensure_daily_note_preamble(DAILY_NOTES_PATH, date_str)

    if has_morning_briefing(note_path) and not force:
        print(f"[daily_briefing_catchup] already present: {note_path.name}")
        return 0

    print(f"[daily_briefing_catchup] running briefing sync for {date_str}")
    result = run_briefing_sync(date_str)
    refreshed = read_text_with_retry(note_path) if note_path.exists() else ""
    if result == 0 and BRIEFING_HEADER in refreshed:
        print(f"[daily_briefing_catchup] briefing written: {note_path.name}")
        return 0
    if "#degraded-sync" in refreshed:
        print(
            f"[daily_briefing_catchup] degraded briefing written; rerun after fixing downstream failure: {note_path.name}",
            file=sys.stderr,
        )
        return result or 1
    print(
        f"[daily_briefing_catchup] briefing sync failed before writing briefing content: {note_path.name}",
        file=sys.stderr,
    )
    return result or 1


def main() -> int:
    args = parse_args()
    return catch_up(args.date, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
