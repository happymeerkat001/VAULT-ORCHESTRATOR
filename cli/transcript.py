#!/usr/bin/env python3
"""
transcript.py - Save transcripts for one or more media URLs into the Obsidian vault.

Usage:
  python3 cli/transcript.py https://www.youtube.com/watch?v=VIDEO_ID
  python3 cli/transcript.py <url1> <url2> ...

Optionally append links into a note:
  python3 cli/transcript.py --append-links-to-note "/path/to/AI Research Log.md" <url1> ...
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from export_transcripts import DEFAULT_OUTPUT_DIR, sanitize_title
from media_captions import fetch_yt_dlp_metadata
from transcript_server import TranscriptService


@dataclass(frozen=True)
class TranscriptResult:
    url: str
    safe_title: str
    transcript_path: str
    transcript_source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Save transcripts for one or more media URLs into the Obsidian vault."
    )
    parser.add_argument("urls", nargs="+", help="Media URLs (YouTube preferred).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write transcript markdown files into (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--append-links-to-note",
        type=Path,
        default=None,
        help="Append [[z.Ingestion/<title>]] links to the given markdown note.",
    )
    return parser.parse_args()


def fetch_media_metadata(url: str) -> tuple[str | None, str | None]:
    payload = fetch_yt_dlp_metadata(url)
    if not payload:
        return None, None

    title = payload.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else None
    description = payload.get("description")
    description = description.strip() if isinstance(description, str) and description.strip() else None
    return title, description


def append_transcript_links(note_path: Path, results: list[TranscriptResult]) -> None:
    if not note_path:
        return
    note_path = note_path.expanduser()
    existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""

    lines: list[str] = []
    lines.append("")
    lines.append("### Transcripts")
    lines.append("")
    for item in results:
        lines.append(f"- [[z.Ingestion/{item.safe_title}]] — {item.url}")

    snippet = "\n".join(lines).strip("\n") + "\n"
    if "### Transcripts" in existing:
        # Avoid duplicating the section: only add bullets that aren't already present.
        updated = existing
        for item in results:
            bullet = f"- [[z.Ingestion/{item.safe_title}]] — {item.url}"
            if bullet not in updated:
                updated = updated.rstrip("\n") + "\n" + bullet + "\n"
        note_path.write_text(updated, encoding="utf-8")
        return

    note_path.write_text(existing.rstrip("\n") + "\n\n" + snippet, encoding="utf-8")


def main() -> int:
    args = parse_args()
    service = TranscriptService(args.output_dir)

    results: list[TranscriptResult] = []
    for raw_url in args.urls:
        url = (raw_url or "").strip()
        if not url:
            continue
        title, description = fetch_media_metadata(url)
        safe_title = sanitize_title(title or url)
        response = service.save_from_url(url=url, title=title, description=description or "", ai_summary="")
        results.append(
            TranscriptResult(
                url=url,
                safe_title=safe_title,
                transcript_path=response.get("path", ""),
                transcript_source=response.get("source", ""),
            )
        )
        print(f"✓ Saved {safe_title}.md ({response.get('source','')})")

    if args.append_links_to_note:
        append_transcript_links(args.append_links_to_note, results)
        print(f"✓ Updated note: {args.append_links_to_note.expanduser()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
