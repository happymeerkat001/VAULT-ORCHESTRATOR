#!/usr/bin/env python3
"""
export_transcripts.py - Export completed Transcript.lol recordings into the Obsidian vault.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py --dry-run
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
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


def extract_youtube_id(url: str) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/")

    if host in {"youtube.com", "m.youtube.com"}:
        if path == "watch":
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            return video_id or None
        if path.startswith("shorts/") or path.startswith("embed/") or path.startswith("live/"):
            parts = path.split("/")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    if host == "youtu.be":
        parts = path.split("/")
        if parts and parts[0]:
            return parts[0]
    return None


def fetch_youtube_transcript(video_id: str) -> str | None:
    if not video_id:
        return None
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    try:
        with tempfile.TemporaryDirectory(prefix="yt-sub-") as temp_dir:
            output_template = str(Path(temp_dir) / f"yt-sub-{video_id}.%(ext)s")
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--write-auto-sub",
                "--sub-lang",
                "en",
                "--skip-download",
                "--sub-format",
                "json3",
                "--js-runtimes",
                "node",
                "-o",
                output_template,
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
                return None

            subtitle_files = sorted(Path(temp_dir).glob("*.json3"))
            if not subtitle_files:
                return None

            data = json.loads(subtitle_files[0].read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            events = data.get("events")
            if not isinstance(events, list):
                return None

            lines: list[str] = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                segs = event.get("segs")
                if not isinstance(segs, list):
                    continue
                parts: list[str] = []
                for seg in segs:
                    if not isinstance(seg, dict):
                        continue
                    text = seg.get("utf8")
                    if isinstance(text, str):
                        cleaned = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
                        if cleaned:
                            parts.append(cleaned)
                if parts:
                    lines.append(" ".join(parts))
            if not lines:
                return None
            return "\n".join(lines)
    except (OSError, json.JSONDecodeError):
        return None


def get_transcript_text(client: TranscriptClient, recording: dict) -> tuple[str, str]:
    recording_id = coalesce_string(recording, "id", "recordingId")
    if not recording_id:
        raise RuntimeError(f"Recording missing id: {recording!r}")

    source = coalesce_string(recording, "source").upper()
    source_url = coalesce_string(recording, "sourceUrl", "url")
    if source == "YOUTUBE":
        video_id = extract_youtube_id(source_url)
        if video_id:
            youtube_text = fetch_youtube_transcript(video_id)
            if youtube_text:
                return youtube_text, "YouTube captions"

    return client.get_transcript(recording_id, "text"), "transcript.lol"


def build_markdown(recording: dict, transcript_text: str, transcript_source: str) -> str:
    title = coalesce_string(recording, "title", "name") or "Untitled"
    source_url = coalesce_string(recording, "sourceUrl", "url")
    created_at = coalesce_string(recording, "createdAt", "created_at", "date")
    language = coalesce_string(recording, "language", "locale")

    return (
        f"# {title}\n\n"
        f"**Source:** {source_url or 'Unknown'}\n"
        f"**Date:** {created_at or 'Unknown'}\n"
        f"**Language:** {language or 'Unknown'}\n"
        f"**Transcript source:** {transcript_source}\n\n"
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

        transcript_text, transcript_source = get_transcript_text(client, recording)
        destination.write_text(
            build_markdown(recording, transcript_text, transcript_source),
            encoding="utf-8",
        )
        written += 1
        print(f"[export] wrote {destination} source={transcript_source}")

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
