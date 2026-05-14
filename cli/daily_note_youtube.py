#!/usr/bin/env python3
"""
daily_note_youtube.py - Ingest bare YouTube URLs from an Obsidian daily note.

Manual run:
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/daily_note_youtube.py
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/daily_note_youtube.py --dry-run
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/daily_note_youtube.py --date 2026-05-12
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import sys

from archive_youtube import fetch_youtube_metadata
from export_transcripts import DEFAULT_OUTPUT_DIR, ensure_daily_note_link, extract_youtube_id, sanitize_title
from transcript_server import TranscriptService

YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s)>\]]+|youtu\.be/[^\s)>\]]+)"
)


@dataclass(frozen=True)
class UrlMatch:
    line_index: int
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest bare YouTube URLs from an Obsidian daily note."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be processed without writing files.",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Daily note date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_OUTPUT_DIR.parent,
        help=f"Obsidian vault root (default: {DEFAULT_OUTPUT_DIR.parent})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write transcript markdown files into (default: <vault-root>/z.Ingestion)",
    )
    return parser.parse_args()


def validate_note_date(raw_value: str) -> str:
    try:
        return date.fromisoformat(raw_value).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid --date value: {raw_value!r}. Expected YYYY-MM-DD.") from exc


def is_bare_youtube_url(line: str, match: re.Match[str]) -> bool:
    before = line[:match.start()]
    after = line[match.end():]

    if "[[" in before and "]]" in after:
        return False
    if before.rstrip().endswith("](") and ")" in after:
        return False
    return True


def find_bare_youtube_urls(note_path: Path) -> list[UrlMatch]:
    if not note_path.exists():
        return []

    matches: list[UrlMatch] = []
    for line_index, line in enumerate(note_path.read_text(encoding="utf-8").splitlines()):
        for match in YOUTUBE_URL_RE.finditer(line):
            if is_bare_youtube_url(line, match):
                matches.append(
                    UrlMatch(
                        line_index=line_index,
                        url=match.group(0).strip(),
                    )
                )
    return matches


def replace_url_line(note_path: Path, line_index: int, url: str, replacement: str) -> bool:
    lines = note_path.read_text(encoding="utf-8").splitlines()
    if line_index >= len(lines):
        return False
    if url not in lines[line_index]:
        return False

    lines[line_index] = lines[line_index].replace(url, replacement)
    updated = "\n".join(lines)
    if note_path.read_text(encoding="utf-8").endswith("\n"):
        updated += "\n"
    note_path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    args = parse_args()
    note_date = validate_note_date(args.date)
    vault_root = args.vault_root.expanduser()
    output_dir = args.output_dir.expanduser() if args.output_dir else vault_root / "z.Ingestion"
    note_path = vault_root / "Daily Notes" / f"{note_date}.md"
    matches = find_bare_youtube_urls(note_path)

    print(f"[daily-note-youtube] note={note_path}")
    print(f"[daily-note-youtube] found {len(matches)} bare YouTube URL(s)")
    if not matches:
        return 0

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    service = TranscriptService(output_dir)
    written = 0
    normalized_existing = 0
    skipped_invalid = 0
    seen_destinations: set[Path] = set()

    for item in matches:
        try:
            video_id = extract_youtube_id(item.url)
            if not video_id:
                skipped_invalid += 1
                print(f"[daily-note-youtube] skip invalid URL: {item.url}")
                continue

            metadata = fetch_youtube_metadata(video_id)
            safe_title = sanitize_title(metadata["title"])
            prefixed_stem = f"*{safe_title}"
            destination = output_dir / f"{prefixed_stem}.md"
            replacement = f"[[z.Ingestion/{prefixed_stem}]]"

            if args.dry_run:
                action = "would normalize existing" if destination.exists() or destination in seen_destinations else "would ingest"
                print(
                    f"[daily-note-youtube] {action} line={item.line_index + 1} "
                    f"title={metadata['title']!r} url={item.url}"
                )
                continue

            if destination.exists() or destination in seen_destinations:
                ensure_daily_note_link(note_path, prefixed_stem, metadata["title"])
                if replace_url_line(note_path, item.line_index, item.url, replacement):
                    normalized_existing += 1
                    print(f"[daily-note-youtube] normalized existing {destination.name}")
                else:
                    print(
                        f"[daily-note-youtube] existing destination but URL no longer present at "
                        f"line={item.line_index + 1}: {destination.name}"
                    )
                seen_destinations.add(destination)
                continue

            response = service.save_from_url(
                url=item.url,
                title=metadata["title"],
                description=metadata["description"],
                mode="full",
                daily_note_path=note_path,
            )
            written += 1
            if replace_url_line(note_path, item.line_index, item.url, replacement):
                print(
                    f"[daily-note-youtube] wrote {destination.name} "
                    f"source={response.get('source', '')}"
                )
            else:
                print(
                    f"[daily-note-youtube] wrote {destination.name} but could not replace URL at "
                    f"line={item.line_index + 1}"
                )
            seen_destinations.add(destination)
        except Exception as exc:
            skipped_invalid += 1
            print(f"[daily-note-youtube] skip {item.url}: {exc}")

    print(
        "[daily-note-youtube] summary: "
        f"written={written} normalized_existing={normalized_existing} "
        f"skipped_invalid={skipped_invalid}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[daily-note-youtube] interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
