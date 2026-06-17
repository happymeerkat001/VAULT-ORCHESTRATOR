#!/usr/bin/env python3
"""
archive_youtube.py - Archive bare YouTube URLs from Obsidian Untitled*.md notes.

Manual run:
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/archive_youtube.py --dry-run
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/archive_youtube.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

from export_transcripts import (
    DEFAULT_OUTPUT_DIR,
    ensure_daily_note_link,
    extract_youtube_id,
    fetch_youtube_transcript,
    sanitize_title,
)
from transcript_lol_summary import prepare_youtube_summary_context
from transcribe import load_env

URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive bare YouTube URLs from Obsidian Untitled*.md notes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be archived without writing or moving files.",
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


def find_untitled_files(vault_root: Path) -> list[Path]:
    paths: set[Path] = set()
    for pattern in ("Untitled*.md", "New Note*.md"):
        for path in vault_root.glob(pattern):
            if path.is_file():
                paths.add(path)
    return sorted(paths)


def extract_url_from_file(path: Path) -> str | None:
    content = path.read_text(encoding="utf-8").strip()
    match = URL_PATTERN.search(content)
    if not match:
        return None
    return match.group(0).strip()


def normalize_date(raw_value: str) -> str:
    cleaned = raw_value.strip()
    if re.fullmatch(r"\d{8}", cleaned):
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned
    return ""


def coalesce_string(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def infer_language(metadata: dict) -> str:
    explicit = coalesce_string(metadata, "language")
    if explicit:
        return explicit

    subtitles = metadata.get("subtitles")
    if isinstance(subtitles, dict):
        for key in subtitles:
            if isinstance(key, str) and key.strip():
                return key.strip()

    automatic_captions = metadata.get("automatic_captions")
    if isinstance(automatic_captions, dict):
        for key in automatic_captions:
            if isinstance(key, str) and key.strip():
                return key.strip()

    return "Unknown"


def fetch_youtube_metadata(video_id: str) -> dict:
    if not video_id:
        raise RuntimeError("Missing YouTube video ID.")

    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        watch_url,
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_text = result.stderr.strip() or result.stdout.strip() or "yt-dlp metadata fetch failed"
        raise RuntimeError(error_text)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Unable to parse yt-dlp metadata JSON.") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected yt-dlp metadata response shape.")

    return {
        "title": coalesce_string(payload, "title") or f"YouTube {video_id}",
        "description": coalesce_string(payload, "description"),
        "upload_date": normalize_date(coalesce_string(payload, "upload_date")),
        "language": infer_language(payload),
        "source_url": coalesce_string(payload, "webpage_url", "original_url") or watch_url,
    }


def build_archive_markdown(
    metadata: dict,
    transcript_text: str,
    transcript_source: str,
    source_url: str,
    ai_summary: str = "",
) -> str:
    title = metadata["title"]
    archive_date = metadata["upload_date"] or date.today().isoformat()
    description = metadata["description"].strip()
    ai_summary_text = ai_summary.strip()
    language = metadata["language"] or "Unknown"

    sections = [
        f"# {title}",
        "",
        f"#[[{archive_date}]]",
        "",
        f"**Source:** {source_url}",
        f"**Date:** {archive_date}",
        f"**Language:** {language}",
        f"**Transcript source:** {transcript_source}",
        "",
    ]

    if description:
        sections.extend(
            [
                "## Description",
                description,
                "",
            ]
        )

    if ai_summary_text:
        sections.extend(
            [
                "## AI Summary",
                ai_summary_text,
                "",
            ]
        )

    sections.extend(
        [
            "# Transcript",
            "---",
            transcript_text.rstrip(),
            "",
        ]
    )

    return "\n".join(sections)


def unique_processed_path(processed_dir: Path, original_name: str) -> Path:
    candidate = processed_dir / original_name
    if not candidate.exists():
        return candidate

    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    index = 1
    while True:
        candidate = processed_dir / f"{stem} {index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def unique_invalid_processed_path(processed_dir: Path, original_name: str) -> Path:
    base = Path(original_name)
    invalid_name = f"{base.stem}.invalid{base.suffix}"
    candidate = processed_dir / invalid_name
    if not candidate.exists():
        return candidate

    index = 1
    while True:
        candidate = processed_dir / f"{base.stem}.invalid {index}{base.suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def main() -> None:
    args = parse_args()
    vault_root = args.vault_root.expanduser()
    output_dir = args.output_dir.expanduser() if args.output_dir else vault_root / "z.Ingestion"
    processed_dir = vault_root / "processed"
    env = load_env()
    untitled_files = find_untitled_files(vault_root)

    print(f"[archive] found {len(untitled_files)} untitled file(s) in {vault_root}")
    if not untitled_files:
        return

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    moved = 0
    skipped_existing = 0
    skipped_invalid = 0

    for source_file in untitled_files:
        try:
            source_url = extract_url_from_file(source_file)
            if not source_url:
                skipped_invalid += 1
                if not args.dry_run:
                    invalid_path = unique_invalid_processed_path(processed_dir, source_file.name)
                    shutil.move(str(source_file), str(invalid_path))
                    moved += 1
                    print(
                        f"[archive] moved invalid {source_file.name} "
                        f"-> processed/{invalid_path.name} (no URL)"
                    )
                else:
                    print(
                        f"[archive] would move invalid {source_file.name} "
                        "-> processed/*.invalid.md (no URL)"
                    )
                continue

            video_id = extract_youtube_id(source_url)
            if not video_id:
                skipped_invalid += 1
                if not args.dry_run:
                    invalid_path = unique_invalid_processed_path(processed_dir, source_file.name)
                    shutil.move(str(source_file), str(invalid_path))
                    moved += 1
                    print(
                        f"[archive] moved invalid {source_file.name} "
                        f"-> processed/{invalid_path.name} (non-YouTube URL)"
                    )
                else:
                    print(
                        f"[archive] would move invalid {source_file.name} "
                        "-> processed/*.invalid.md (non-YouTube URL)"
                    )
                continue

            metadata = fetch_youtube_metadata(video_id)
            safe_title = sanitize_title(metadata["title"])
            daily_note_path = vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"
            processed_path = unique_processed_path(processed_dir, source_file.name)
            archive_date = metadata["upload_date"] or date.today().isoformat()

            # Check both possible destinations before we know the transcript source
            existing_destination = next(
                (output_dir / f"{prefix}{safe_title}.md" for prefix in ("*", "") if (output_dir / f"{prefix}{safe_title}.md").exists()),
                None,
            )
            if existing_destination:
                skipped_existing += 1
                if not args.dry_run:
                    shutil.move(str(source_file), str(processed_path))
                    moved += 1
                    print(
                        f"[archive] skip existing {existing_destination.name}, "
                        f"moved {source_file.name} -> processed/{processed_path.name}"
                    )
                else:
                    print(
                        f"[archive] would skip existing {existing_destination.name}, "
                        f"would move {source_file.name} -> processed/{processed_path.name}"
                )
                continue

            summary_context = None
            ai_summary = ""
            if not args.dry_run:
                summary_context = prepare_youtube_summary_context(
                    source_url,
                    metadata["title"],
                    env=env,
                    timeout_seconds=600,
                )
                ai_summary = summary_context.summary

            if args.dry_run:
                print(
                    f"[archive] would write {safe_title}.md "
                    f"from {source_file.name} date={archive_date}"
                )
                print(f"[archive] would move {source_file.name} -> processed/{processed_path.name}")
                continue

            transcript_text = fetch_youtube_transcript(video_id, include_timestamps=True)
            transcript_source = "YouTube captions"
            if not transcript_text and summary_context.client and summary_context.recording_id:
                transcript_text = summary_context.client.get_transcript(summary_context.recording_id, "text")
                transcript_source = "transcript.lol"
            if not transcript_text:
                raise RuntimeError("No transcript returned from YouTube captions.")

            stem_prefix = "" if transcript_source == "transcript.lol" else "*"
            prefixed_stem = f"{stem_prefix}{safe_title}"
            destination = output_dir / f"{prefixed_stem}.md"

            destination.write_text(
                build_archive_markdown(
                    metadata,
                    transcript_text,
                    transcript_source,
                    metadata["source_url"] or source_url,
                    ai_summary=ai_summary,
                ),
                encoding="utf-8",
            )
            ensure_daily_note_link(daily_note_path, prefixed_stem)
            shutil.move(str(source_file), str(processed_path))
            written += 1
            moved += 1
            print(f"[archive] wrote {destination}")
            print(f"[archive] moved {source_file.name} -> processed/{processed_path.name}")
        except Exception as exc:
            skipped_invalid += 1
            if not args.dry_run and source_file.exists():
                invalid_path = unique_invalid_processed_path(processed_dir, source_file.name)
                shutil.move(str(source_file), str(invalid_path))
                moved += 1
                print(
                    f"[archive] moved invalid {source_file.name} "
                    f"-> processed/{invalid_path.name}: {exc}"
                )
            else:
                print(f"[archive] skip {source_file.name}: {exc}")

    print(
        "[archive] summary: "
        f"written={written} moved={moved} "
        f"skipped_existing={skipped_existing} skipped_invalid={skipped_invalid}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[archive] interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
