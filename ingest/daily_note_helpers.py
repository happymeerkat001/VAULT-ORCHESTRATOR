"""Shared helpers for Obsidian Daily Note creation and normalization."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path


def read_text_with_retry(
    path: Path,
    attempts: int = 30,
    initial_delay: float = 0.5,
    max_delay: float = 4.0,
) -> str:
    """Read UTF-8 text, retrying transient iCloud file-lock failures."""
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
    """Write UTF-8 text, retrying transient iCloud file-lock failures."""
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(min(initial_delay * 2**i, max_delay))
    raise last_exc or OSError(f"Unable to write {path}")


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


def daily_note_days_line(date_str: str) -> str:
    current = datetime.strptime(date_str, "%Y-%m-%d")
    prev_date = (current - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (current + timedelta(days=1)).strftime("%Y-%m-%d")
    return (
        f"Days:[[Daily Notes/{prev_date} | Yesterday]] <== [[Daily Notes/{date_str}]] ==> "
        f"[[Daily Notes/{next_date}|Tomorrow]]"
    )


def has_note_preamble(content: str) -> bool:
    head = "\n".join(content.splitlines()[:20])
    return head.startswith("---\n") and "tags:" in head and "Days:[[" in head


def ensure_daily_note_preamble(daily_notes_path: Path, date_str: str) -> Path:
    """Ensure a Daily Note exists and has the canonical preamble.

    If a note already has YAML frontmatter but no Days navigation, insert only
    the missing Days line after the closing frontmatter delimiter. This avoids
    the duplicate-frontmatter repair artifact that can happen when another
    workflow creates a frontmatter-only stub before the briefing sync runs.
    """
    note_path = daily_notes_path / f"{date_str}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)

    if not note_path.exists():
        write_text_with_retry(note_path, build_note_preamble(date_str))
        return note_path

    existing = read_text_with_retry(note_path)
    if has_note_preamble(existing):
        return note_path

    days_line = daily_note_days_line(date_str)
    if existing.startswith("---\n"):
        match = re.match(r"\A(---\n[\s\S]*?\n---\n?)([\s\S]*)\Z", existing)
        if match:
            frontmatter, body = match.groups()
            normalized = f"{frontmatter.rstrip()}\n{days_line}\n{body.lstrip()}"
            write_text_with_retry(note_path, normalized)
            return note_path

    normalized = f"{build_note_preamble(date_str)}\n{existing.lstrip()}"
    write_text_with_retry(note_path, normalized)
    return note_path
