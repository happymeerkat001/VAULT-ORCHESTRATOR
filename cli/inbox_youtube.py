#!/usr/bin/env python3
"""Ingest bare YouTube URLs from Obsidian Mobile Share Sheet Inbox notes."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from archive_youtube import fetch_youtube_metadata
from daily_note_youtube import YOUTUBE_URL_RE
from export_transcripts import (
    DEFAULT_OUTPUT_DIR,
    build_markdown,
    ensure_daily_note_link,
    extract_youtube_id,
    resolve_youtube_marker,
    sanitize_title,
    youtube_ingest_stem,
)
from scrape_notes import (
    is_transient_lock_error,
    read_text_with_retry,
    remove_succeeded_youtube_urls,
    unique_processed_path,
    write_text_with_retry,
)
from transcript_server import TranscriptService


AI_SUMMARY_HEADING_RE = re.compile(r"^#{1,6}\s+AI Summary\s*$", re.IGNORECASE)
TRANSCRIPT_HEADING_RE = re.compile(r"^#{1,6}\s+(?:YouTube )?Transcript\s*$", re.IGNORECASE)
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest bare YouTube URLs from Obsidian Share Sheet Inbox notes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be processed without writing or moving files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess URLs even when a matching transcript note already exists.",
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


def is_ingestable_youtube_url(line: str, match: re.Match[str]) -> bool:
    """Return True if the matched URL should be treated as unprocessed.

    Bare URLs are ingestable, as are URLs embedded as markdown image syntax
    (``![](url)``) -- the current Obsidian iOS Share Sheet format. A plain
    markdown link (``[text](url)``) or wikilink (``[[...]]``) around the URL
    means it is already linked elsewhere (e.g. a prior successful ingest) and
    should be left alone.
    """
    before = line[:match.start()]
    after = line[match.end():]

    if "[[" in before and "]]" in after:
        return False

    stripped_before = before.rstrip()
    if stripped_before.endswith("](") and ")" in after:
        bracket_start = stripped_before.rfind("[")
        is_image = bracket_start > 0 and stripped_before[bracket_start - 1] == "!"
        return is_image

    return True


def extract_bare_youtube_urls(content: str) -> list[str]:
    urls: list[str] = []
    for line in content.splitlines():
        for match in YOUTUBE_URL_RE.finditer(line):
            if is_ingestable_youtube_url(line, match):
                urls.append(match.group(0).strip())
    return urls


def extract_ai_summary(content: str) -> str:
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        if not AI_SUMMARY_HEADING_RE.match(line.strip()):
            continue
        summary_lines: list[str] = []
        for tail in lines[idx + 1 :]:
            if MARKDOWN_HEADING_RE.match(tail.strip()):
                break
            summary_lines.append(tail)
        return "\n".join(summary_lines).strip()
    return ""


def extract_local_transcript(content: str) -> str:
    """Return a pre-extracted transcript already embedded in a shared note.

    The current Obsidian iOS Share Sheet format captures a full transcript
    under a ``## Transcript`` (or ``## YouTube Transcript``) heading at share
    time. When present, this is used directly instead of re-fetching via
    transcript.lol or the YouTube captions API.
    """
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        if not TRANSCRIPT_HEADING_RE.match(line.strip()):
            continue
        transcript_lines: list[str] = []
        for tail in lines[idx + 1 :]:
            if MARKDOWN_HEADING_RE.match(tail.strip()):
                break
            transcript_lines.append(tail)
        return "\n".join(transcript_lines).strip()
    return ""


def existing_destination(output_dir: Path, title: str) -> Path | None:
    safe_title = sanitize_title(title)
    for marker in ("", "*", "Txnlol F-YT Only "):
        candidate = output_dir / f"{youtube_ingest_stem(safe_title, marker=marker)}.md"
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    args = parse_args()
    vault_root = args.vault_root.expanduser()
    output_dir = args.output_dir.expanduser() if args.output_dir else vault_root / "z.Ingestion"
    inbox_dir = vault_root / "Inbox"
    source_dir = vault_root / "z.Ingestion"
    processed_dir = vault_root / "processed"
    source_dir_resolved = source_dir.resolve()
    source_files = sorted(
        {path for directory in (inbox_dir, source_dir) for path in directory.glob("*.md") if path.is_file()},
        key=lambda path: str(path),
    )

    print(f"[inbox-youtube] found {len(source_files)} source note(s) in {inbox_dir} and {source_dir}")
    if not source_files:
        return 0

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)

    service: TranscriptService | None = None
    written = 0
    normalized_existing = 0
    moved = 0
    failures = 0

    for source_path in source_files:
        try:
            content = read_text_with_retry(source_path)
        except OSError as exc:
            failures += 1
            label = "transient error" if is_transient_lock_error(exc) else "read error"
            print(
                f"[inbox-youtube] {label}; leaving {source_path.name} in Inbox for retry: {exc}",
                file=sys.stderr,
            )
            continue

        urls = extract_bare_youtube_urls(content)
        if not urls:
            continue
        ai_summary = extract_ai_summary(content)
        local_transcript = extract_local_transcript(content)

        succeeded_urls: set[str] = set()
        failed = False
        daily_note_path = vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"
        source_is_ingestion = source_path.resolve().parent == source_dir_resolved
        for url in urls:
            try:
                video_id = extract_youtube_id(url)
                if not video_id:
                    raise RuntimeError(f"Invalid YouTube URL: {url}")
                metadata = fetch_youtube_metadata(video_id)
                destination = existing_destination(output_dir, metadata["title"])

                if args.dry_run:
                    if destination and not args.force:
                        action = "would normalize existing"
                    elif local_transcript:
                        action = "would ingest from local transcript"
                    else:
                        action = "would ingest via transcript.lol"
                    print(
                        f"[inbox-youtube] {action} {source_path.name}: "
                        f"title={metadata['title']!r} url={url}"
                    )
                    continue

                if destination and not args.force:
                    ensure_daily_note_link(daily_note_path, destination.stem, metadata["title"])
                    normalized_existing += 1
                    succeeded_urls.add(url)
                    print(f"[inbox-youtube] normalized existing {destination.name}")
                    continue

                if local_transcript:
                    marker = resolve_youtube_marker(
                        mode="full", has_ai_summary=False, used_transcript_lol=False
                    )
                    stem = youtube_ingest_stem(metadata["title"], marker=marker)
                    note_body = build_markdown(
                        {"title": metadata["title"], "sourceUrl": url},
                        local_transcript,
                        "YouTube Share Sheet",
                        description=metadata["description"],
                    )
                    note_path = output_dir / f"{stem}.md"
                    write_text_with_retry(note_path, note_body)
                    ensure_daily_note_link(daily_note_path, stem, metadata["title"])
                    succeeded_urls.add(url)
                    written += 1
                    print(f"[inbox-youtube] wrote {note_path.name} (local transcript)")
                    continue

                if service is None:
                    service = TranscriptService(output_dir)
                response = service.save_from_url(
                    url=url,
                    title=metadata["title"],
                    description=metadata["description"],
                    ai_summary=ai_summary,
                    mode="full",
                    daily_note_path=daily_note_path,
                )
                actual_stem = response.get("stem") or Path(response["path"]).stem
                ensure_daily_note_link(daily_note_path, actual_stem, metadata["title"])
                succeeded_urls.add(url)
                written += 1
                print(f"[inbox-youtube] wrote {Path(response['path']).name}")
            except Exception as exc:
                failures += 1
                failed = True
                reason = str(exc).strip() or exc.__class__.__name__
                print(
                    f"[inbox-youtube] failed {source_path.name}: {url} ({reason}); leaving source in Inbox",
                    file=sys.stderr,
                )

        if args.dry_run or not succeeded_urls:
            continue

        updated_content = remove_succeeded_youtube_urls(content, succeeded_urls)
        should_move_source = source_is_ingestion or not updated_content
        if failed or updated_content and not should_move_source:
            try:
                write_text_with_retry(
                    source_path,
                    f"{updated_content}\n" if updated_content else "",
                )
            except OSError as exc:
                failures += 1
                print(
                    f"[inbox-youtube] could not update {source_path.name}; leaving source in Inbox: {exc}",
                    file=sys.stderr,
                )
            continue

        try:
            if updated_content:
                write_text_with_retry(
                    source_path,
                    f"{updated_content}\n",
                )
            processed_path = unique_processed_path(processed_dir, source_path.name)
            shutil.move(str(source_path), str(processed_path))
            moved += 1
            print(f"[inbox-youtube] moved {source_path.name} -> processed/{processed_path.name}")
        except OSError as exc:
            failures += 1
            print(
                f"[inbox-youtube] could not move {source_path.name}; leaving source in Inbox: {exc}",
                file=sys.stderr,
            )

    print(
        "[inbox-youtube] summary: "
        f"written={written} normalized_existing={normalized_existing} moved={moved} failures={failures}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[inbox-youtube] interrupted", file=sys.stderr)
        raise SystemExit(130)
