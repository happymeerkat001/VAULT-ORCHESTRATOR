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
import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from export_transcripts import DEFAULT_OUTPUT_DIR, extract_youtube_id, sanitize_title
from transcript_server import TranscriptService
from transcribe import load_env

MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"


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


def fetch_youtube_metadata(url: str) -> tuple[str | None, str | None]:
    """Return (title, description) for a YouTube URL via yt-dlp."""
    video_id = extract_youtube_id(url)
    if not video_id:
        return None, None

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
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
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, None

    if not isinstance(data, dict):
        return None, None

    title = data.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else None
    description = data.get("description")
    description = description.strip() if isinstance(description, str) and description.strip() else None
    return title, description


def generate_ai_summary(description: str, minimax_api_key: str) -> str:
    """Generate a 2-3 sentence AI summary of the video description via MiniMax."""
    body = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are summarizing a YouTube video for an Obsidian note. "
                    "Write 2-3 concise sentences describing what the video covers and its key takeaways. "
                    "No markdown formatting, no bullet points. Plain prose only."
                ),
            },
            {"role": "user", "content": f"Video description:\n\n{description[:3000]}"},
        ],
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        MINIMAX_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {minimax_api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
        return content.strip()
    except Exception:
        return ""


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

    env = load_env()
    minimax_api_key = env.get("MINIMAX_API_KEY", "").strip()

    results: list[TranscriptResult] = []
    for raw_url in args.urls:
        url = (raw_url or "").strip()
        if not url:
            continue
        title, description = fetch_youtube_metadata(url)
        ai_summary = ""
        if description and minimax_api_key:
            ai_summary = generate_ai_summary(description, minimax_api_key)
        safe_title = sanitize_title(title or url)
        response = service.save_from_url(url=url, title=title, description=description or "", ai_summary=ai_summary)
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
