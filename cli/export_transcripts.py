#!/usr/bin/env python3
"""
export_transcripts.py - Export completed Transcript.lol recordings into the Obsidian vault.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py --dry-run
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/export_transcripts.py
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from transcribe import API_BASE_URL, TERMINAL_STATUSES, TranscriptClient, extract_status, load_env

DEFAULT_OUTPUT_DIR = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator/z.Ingestion"
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


def fetch_youtube_transcript(video_id: str, include_timestamps: bool = False) -> str | None:
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

            return parse_json3_transcript(subtitle_files[0], include_timestamps=include_timestamps)
    except (OSError, json.JSONDecodeError):
        return None


def format_timestamp(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_json3_transcript(json3_path: Path, include_timestamps: bool = False) -> str | None:
    data = json.loads(json3_path.read_text(encoding="utf-8"))
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
            line = " ".join(parts)
            if include_timestamps:
                start_ms = event.get("tStartMs")
                if isinstance(start_ms, int):
                    line = f"[{format_timestamp(start_ms)}] {line}"
            lines.append(line)
    if not lines:
        return None
    return "\n".join(lines)


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


def build_markdown(
    recording: dict,
    transcript_text: str,
    transcript_source: str,
    description: str = "",
    ai_summary: str = "",
) -> str:
    title = coalesce_string(recording, "title", "name") or "Untitled"
    source_url = coalesce_string(recording, "sourceUrl", "url")
    created_at = coalesce_string(recording, "createdAt", "created_at", "date")
    language = coalesce_string(recording, "language", "locale")
    today_tag = date.today().isoformat()
    description_text = description.strip()
    ai_summary_text = ai_summary.strip()

    optional_sections = ""
    if description_text:
        optional_sections += f"## Description\n\n{description_text}\n\n"
    if ai_summary_text:
        optional_sections += f"## AI Summary\n\n{ai_summary_text}\n\n"

    return (
        f"# {title}\n\n"
        f"**Source:** {source_url or 'Unknown'}\n"
        f"**Date:** {created_at or 'Unknown'}\n"
        f"**Language:** {language or 'Unknown'}\n"
        f"**Transcript source:** {transcript_source}\n\n"
        f"{optional_sections}"
        "---\n\n"
        f"{transcript_text.rstrip()}\n\n"
        f"#{today_tag}\n"
    )


def is_exportable_status(status: str) -> bool:
    normalized = status.strip().upper()
    if normalized in EXPORTABLE_STATUSES:
        return True
    return normalized.endswith("_COMPLETE") or normalized.endswith("_COMPLETED")


def ensure_daily_note_link(
    daily_note_path: Path,
    transcript_title: str,
    display_title: str | None = None,
) -> None:
    visible_title = (display_title or transcript_title).strip()
    link = f"[[z.Ingestion/*{transcript_title}]]"
    block = f"### {visible_title}\n{link}"
    daily_note_path.parent.mkdir(parents=True, exist_ok=True)
    if not daily_note_path.exists():
        daily_note_path.write_text("", encoding="utf-8")
    content = daily_note_path.read_text(encoding="utf-8")
    if link in content:
        return

    to_append = f"\n{block}\n" if content and not content.endswith("\n") else f"{block}\n"
    with daily_note_path.open("a", encoding="utf-8") as handle:
        handle.write(to_append)


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.expanduser()
    vault_root = output_dir.parent
    daily_note_path = vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"

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
        destination = output_dir / f"*{safe_title}.md"

        if not is_exportable_status(status):
            skipped_incomplete += 1
            print(f"[export] skip incomplete status={status} title={title}")
            continue

        legacy_destination = output_dir / f"{safe_title}.md"
        if destination.exists() or legacy_destination.exists():
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
        ensure_daily_note_link(daily_note_path, safe_title, title)
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
