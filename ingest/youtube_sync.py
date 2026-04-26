#!/usr/bin/env python3
"""
youtube_sync.py — Fetches YouTube transcript + summary and appends to Obsidian daily note.

No Google OAuth required. Uses public YouTube watch/caption endpoints.

Examples:
  python3 ingest/youtube_sync.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  python3 ingest/youtube_sync.py --url "https://youtu.be/dQw4w9WgXcQ" --date 2026-04-24
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
VAULT_PATH = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-Orchestrator"
).expanduser()
LOCAL_TIMEZONE = "America/Chicago"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# ─────────────────────────────────────────────────────────────────────────────


def today_local() -> str:
    if _HAS_ZONEINFO:
        return datetime.now(tz=ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape transcript + summary from a YouTube video and append to daily note."
    )
    parser.add_argument("--url", required=True, help="YouTube video URL.")
    parser.add_argument(
        "--date",
        help="Daily note date in YYYY-MM-DD. Defaults to today in local timezone.",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Preferred caption language code (default: en).",
    )
    parser.add_argument(
        "--summary-sentences",
        type=int,
        default=5,
        help="Summary sentence count (default: 5).",
    )
    return parser.parse_args()


def fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_video_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/")
        if vid:
            return vid
    if "youtube.com" in parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query and query["v"]:
            return query["v"][0]
        path_match = re.match(r"^/(embed|shorts)/([^/?]+)", parsed.path)
        if path_match:
            return path_match.group(2)
    raise ValueError(f"Unsupported YouTube URL: {url}")


def extract_player_response_html(watch_html: str) -> dict:
    marker = "ytInitialPlayerResponse = "
    idx = watch_html.find(marker)
    if idx == -1:
        raise RuntimeError("Unable to find ytInitialPlayerResponse in watch page.")
    start = idx + len(marker)
    end = watch_html.find(";</script>", start)
    if end == -1:
        raise RuntimeError("Unable to parse ytInitialPlayerResponse JSON boundary.")
    payload = watch_html[start:end].strip()
    return json.loads(payload)


def choose_caption_track(caption_tracks: list[dict], preferred_lang: str) -> dict:
    if not caption_tracks:
        raise RuntimeError("No caption tracks available for this video.")
    for track in caption_tracks:
        if track.get("languageCode") == preferred_lang:
            return track
    for track in caption_tracks:
        if (track.get("languageCode") or "").startswith(preferred_lang):
            return track
    return caption_tracks[0]


def fetch_transcript_from_track(track: dict) -> str:
    base_url = track.get("baseUrl")
    if not base_url:
        raise RuntimeError("Caption track missing baseUrl.")
    transcript_xml = fetch_text(base_url)
    root = ET.fromstring(transcript_xml)
    chunks: list[str] = []
    for node in root.findall(".//text"):
        segment = html.unescape("".join(node.itertext())).strip()
        if segment:
            chunks.append(re.sub(r"\s+", " ", segment))
    if not chunks:
        raise RuntimeError("Caption track returned empty transcript.")
    return "\n".join(chunks)


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return [p.strip() for p in parts if p.strip()]


def summarize_transcript(transcript: str, max_sentences: int) -> str:
    sentences = split_sentences(transcript)
    if not sentences:
        return "No transcript text available to summarize."
    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "that",
        "this",
        "to",
        "of",
        "for",
        "in",
        "on",
        "with",
        "is",
        "it",
        "as",
        "at",
        "by",
        "be",
        "are",
        "was",
        "were",
        "from",
    }
    words = re.findall(r"[a-zA-Z']+", transcript.lower())
    freq: dict[str, int] = {}
    for word in words:
        if len(word) < 3 or word in stopwords:
            continue
        freq[word] = freq.get(word, 0) + 1
    if not freq:
        return " ".join(sentences[:max_sentences])

    scored: list[tuple[int, int, str]] = []
    for idx, sentence in enumerate(sentences):
        sent_words = re.findall(r"[a-zA-Z']+", sentence.lower())
        score = sum(freq.get(w, 0) for w in sent_words)
        scored.append((score, idx, sentence))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:max_sentences]
    top_sorted = sorted(top, key=lambda x: x[1])
    return " ".join(item[2] for item in top_sorted)


def build_markdown(title: str, url: str, language: str, summary: str, transcript: str) -> str:
    return (
        f"\n### {title}\n"
        f"URL: {url}\n"
        f"Language: {language}\n\n"
        f"#### Summary\n{summary}\n\n"
        f"#### Transcript\n{transcript}\n"
    )


def append_to_note(date_str: str, block: str) -> Path:
    note_path = VAULT_PATH / f"{date_str}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    has_section = note_path.exists() and "## YouTube" in note_path.read_text(encoding="utf-8")
    prefix = "" if has_section else "\n## YouTube\n"
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(prefix + block)
    return note_path


def main() -> None:
    args = parse_args()
    date_str = args.date or today_local()
    video_id = extract_video_id(args.url)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"[youtube_sync] date={date_str} video_id={video_id}")
    watch_html = fetch_text(watch_url)
    player = extract_player_response_html(watch_html)

    details = player.get("videoDetails") or {}
    title = (details.get("title") or f"YouTube Video {video_id}").strip()
    tracks = (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    track = choose_caption_track(tracks, args.lang)
    language = track.get("languageCode") or "unknown"

    transcript = fetch_transcript_from_track(track)
    summary = summarize_transcript(transcript, max_sentences=max(1, args.summary_sentences))

    md = build_markdown(title=title, url=watch_url, language=language, summary=summary, transcript=transcript)
    note_path = append_to_note(date_str, md)
    print(f"[youtube_sync] appended transcript+summary to {note_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
