#!/usr/bin/env python3
"""
export_transcripts.py - Export completed Transcript.lol recordings into the Obsidian vault.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py --dry-run
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from transcribe import API_BASE_URL, TERMINAL_STATUSES, TranscriptClient, extract_status, load_env

DEFAULT_OUTPUT_DIR = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator/Transcripts"
).expanduser()
EXPORTABLE_STATUSES = TERMINAL_STATUSES | {
    "TRANSCRIPTION_COMPLETE",
    "TRANSCRIPT_COMPLETE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export completed Transcript.lol recordings into the Obsidian vault."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be exported without writing files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write markdown files into (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args()


def list_recordings(client: TranscriptClient) -> list[dict]:
    data = client._json_request(f"{API_BASE_URL}/spaces/{client.space_id}/recordings")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("recordings", "items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            raise RuntimeError(f"Unexpected recordings response shape: {data!r}")
    else:
        raise RuntimeError(f"Unexpected recordings response type: {type(data).__name__}")

    recordings = [item for item in items if isinstance(item, dict)]
    return recordings


def sanitize_title(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(".")
    return cleaned or "untitled"


def coalesce_string(recording: dict, *keys: str) -> str:
    for key in keys:
        value = recording.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_markdown(recording: dict, transcript_text: str) -> str:
    title = coalesce_string(recording, "title", "name") or "Untitled"
    source_url = coalesce_string(recording, "sourceUrl", "url")
    created_at = coalesce_string(recording, "createdAt", "created_at", "date")
    language = coalesce_string(recording, "language", "locale")

    return (
        f"# {title}\n\n"
        f"**Source:** {source_url or 'Unknown'}\n"
        f"**Date:** {created_at or 'Unknown'}\n"
        f"**Language:** {language or 'Unknown'}\n\n"
        "---\n\n"
        f"{transcript_text.rstrip()}\n"
    )


def is_exportable_status(status: str) -> bool:
    normalized = status.strip().upper()
    if normalized in EXPORTABLE_STATUSES:
        return True
    return normalized.endswith("_COMPLETE") or normalized.endswith("_COMPLETED")


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.expanduser()

    env = load_env()
    client = TranscriptClient(env)
    client.authenticate()

    recordings = list_recordings(client)
    print(f"[export] found {len(recordings)} recording(s) in space_id={client.space_id}")

    exportable = 0
    written = 0
    skipped_existing = 0
    skipped_incomplete = 0

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for recording in recordings:
        recording_id = coalesce_string(recording, "id", "recordingId")
        title = coalesce_string(recording, "title", "name") or f"recording-{recording_id or 'unknown'}"
        status = extract_status(recording)
        safe_title = sanitize_title(title)
        destination = output_dir / f"{safe_title}.md"

        if not is_exportable_status(status):
            skipped_incomplete += 1
            print(f"[export] skip incomplete status={status} title={title}")
            continue

        if destination.exists():
            skipped_existing += 1
            print(f"[export] skip existing {destination.name}")
            continue

        exportable += 1
        if args.dry_run:
            print(f"[export] would export {destination.name}")
            continue

        if not recording_id:
            raise RuntimeError(f"Recording missing id: {recording!r}")

        transcript_text = client.get_transcript(recording_id, "text")
        destination.write_text(build_markdown(recording, transcript_text), encoding="utf-8")
        written += 1
        print(f"[export] wrote {destination}")

    print(
        "[export] summary: "
        f"exportable={exportable} written={written} "
        f"skipped_existing={skipped_existing} skipped_incomplete={skipped_incomplete}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[export] interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
