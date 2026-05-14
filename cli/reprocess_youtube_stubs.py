#!/usr/bin/env python3
"""
reprocess_youtube_stubs.py - Reprocess URL-only YouTube ingest stubs in z.Ingestion/.

Manual run:
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/reprocess_youtube_stubs.py --dry-run
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/reprocess_youtube_stubs.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from archive_youtube import fetch_youtube_metadata
from daily_note_youtube import YOUTUBE_URL_RE
from export_transcripts import DEFAULT_OUTPUT_DIR, extract_youtube_id
from transcript_server import TranscriptService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reprocess URL-only YouTube stub files in z.Ingestion/."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List stub files and URLs without writing or deleting files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to scan and write markdown files into (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args()


def read_stub_urls(path: Path) -> list[str]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    urls: list[str] = []

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = YOUTUBE_URL_RE.fullmatch(stripped)
        if not match:
            return []
        urls.append(match.group(0))

    return urls


def find_stub_files(output_dir: Path) -> list[tuple[Path, list[str]]]:
    stubs: list[tuple[Path, list[str]]] = []
    for path in sorted(output_dir.glob("*.md")):
        if not path.is_file():
            continue
        urls = read_stub_urls(path)
        if urls:
            stubs.append((path, urls))
    return stubs


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser()
    stubs = find_stub_files(output_dir)
    url_count = sum(len(urls) for _, urls in stubs)

    print(f"[reprocess-youtube-stubs] output_dir={output_dir}")
    print(f"[reprocess-youtube-stubs] found {len(stubs)} stub file(s), {url_count} URL(s)")
    if not stubs:
        return 0

    if args.dry_run:
        for path, urls in stubs:
            for url in urls:
                print(f"[reprocess-youtube-stubs] would process {path.name}: {url}")
        return 0

    service = TranscriptService(output_dir)
    processed_files = 0
    processed_urls = 0
    failures = 0

    for path, urls in stubs:
        file_failed = False
        for url in urls:
            try:
                video_id = extract_youtube_id(url)
                if not video_id:
                    raise RuntimeError(f"Invalid YouTube URL: {url}")
                metadata = fetch_youtube_metadata(video_id)
                service.save_from_url(
                    url=url,
                    title=metadata["title"],
                    description=metadata["description"],
                    mode="full",
                )
                processed_urls += 1
                print(f"[reprocess-youtube-stubs] processed {path.name}: {metadata['title']}")
            except Exception as exc:
                file_failed = True
                failures += 1
                print(f"[reprocess-youtube-stubs] ERROR {path.name}: {exc}", file=sys.stderr)

        if file_failed:
            continue

        path.unlink()
        processed_files += 1
        print(f"[reprocess-youtube-stubs] deleted stub {path.name}")

    print(
        "[reprocess-youtube-stubs] summary: "
        f"processed_files={processed_files} processed_urls={processed_urls} failures={failures}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[reprocess-youtube-stubs] interrupted", file=sys.stderr)
        raise SystemExit(130)
