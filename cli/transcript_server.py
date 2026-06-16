#!/usr/bin/env python3
"""
transcript_server.py - Local HTTP bridge for Chrome extension transcript saves.

Run:
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/transcript_server.py
"""

from __future__ import annotations

import json
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from export_transcripts import (
    DEFAULT_OUTPUT_DIR,
    append_text_with_retry,
    build_markdown,
    ensure_daily_note_link,
    extract_youtube_id,
    fetch_youtube_transcript,
    sanitize_title,
)
from media_captions import fetch_vimeo_captions
from transcribe import (
    TranscriptClient,
    detect_media_type,
    detect_source,
    load_env,
    wait_for_transcript,
)
from transcript_lol_summary import prepare_youtube_summary_context

HOST = "127.0.0.1"
PORT = 8765
FALLBACK_TIMEOUT_SECONDS = 600


def read_text_with_retry(path: Path, attempts: int = 10, delay_s: float = 0.5) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last_exc = exc
            time.sleep(delay_s)
    raise last_exc or OSError(f"Unable to read {path}")


def write_text_with_retry(path: Path, content: str, attempts: int = 10, delay_s: float = 1.0) -> None:
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(delay_s)
    raise last_exc or OSError(f"Unable to write {path}")


class TranscriptService:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.expanduser()
        self.vault_root = self.output_dir.parent
        self.env = load_env()
        self.client: TranscriptClient | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save_from_url(
        self,
        url: str,
        title: str | None,
        description: str = "",
        ai_summary: str = "",
        mode: str = "full",
        daily_note_path: Path | None = None,
    ) -> dict[str, str]:
        cleaned_url = (url or "").strip()
        if not cleaned_url:
            raise ValueError("Missing required field: url")
        normalized_mode = mode.strip().lower() if isinstance(mode, str) else "full"
        if normalized_mode not in {"full", "youtube"}:
            raise ValueError(f"Invalid mode: {mode}")

        default_title = title.strip() if isinstance(title, str) and title.strip() else cleaned_url
        safe_title = sanitize_title(default_title)
        destination = self.output_dir / f"*{safe_title}.md"
        target_daily_note_path = (
            daily_note_path.expanduser()
            if isinstance(daily_note_path, Path)
            else self.vault_root / "Daily Notes" / f"{date.today().isoformat()}.md"
        )

        transcript_text: str | None = None
        transcript_source = "transcript.lol"
        source = detect_source(cleaned_url)
        summary_context = None

        if source == "YOUTUBE":
            video_id = extract_youtube_id(cleaned_url)
            if video_id:
                transcript_text = fetch_youtube_transcript(video_id)
                if transcript_text:
                    transcript_source = "YouTube captions"
                elif normalized_mode == "youtube":
                    raise RuntimeError("No YouTube captions available for this video")

                if normalized_mode == "full":
                    summary_context = prepare_youtube_summary_context(
                        cleaned_url,
                        default_title,
                        env=self.env,
                        client=self.client,
                        timeout_seconds=FALLBACK_TIMEOUT_SECONDS,
                    )
                    if summary_context.client is not None:
                        self.client = summary_context.client
                    ai_summary = summary_context.summary or ai_summary
                    if summary_context.recording_id and summary_context.summary:
                        print(
                            f"[transcript_server] using Transcript.lol summary for recording {summary_context.recording_id}"
                        )
                    elif ai_summary:
                        print("[transcript_server] using YouTube native summary (Transcript.lol unavailable)")
                    if not transcript_text and summary_context.client and summary_context.recording_id:
                        transcript_text = summary_context.client.get_transcript(summary_context.recording_id, "text")
                        transcript_source = "transcript.lol"
        elif source == "VIMEO":
            transcript_text = fetch_vimeo_captions(cleaned_url, "en")
            if transcript_text:
                transcript_source = "Vimeo captions"

        if not transcript_text:
            if normalized_mode == "youtube":
                raise RuntimeError("YouTube-only mode is supported only for YouTube videos with captions")
            try:
                transcript_text = self._fetch_from_transcript_lol(cleaned_url, default_title, source)
            except Exception as exc:
                if source == "VIMEO":
                    raise RuntimeError(
                        f"No Vimeo captions found; Transcript.lol media import failed. {exc}"
                    ) from exc
                raise
            transcript_source = "transcript.lol"

        metadata = {
            "title": default_title,
            "sourceUrl": cleaned_url,
            "createdAt": date.today().isoformat(),
            "language": "en",
        }
        markdown_content = build_markdown(
            metadata,
            transcript_text,
            transcript_source,
            description=description,
            ai_summary=ai_summary,
        )
        write_text_with_retry(destination, markdown_content)
        print(f"[transcript_server] wrote {destination.name}: has_description={bool(description)}, has_ai_summary={bool(ai_summary)}, md_includes_description={'## Description' in markdown_content}, md_includes_ai_summary={'## AI Summary' in markdown_content}")
        ensure_daily_note_link(target_daily_note_path, f"*{safe_title}", default_title)

        summary_failure = summary_context.summary_failure if summary_context else ""
        if summary_failure:
            short_reason = summary_failure.split("\n")[0][:200]
            callout = f"> [!WARNING] Transcript.lol summary failed: {short_reason}\n\n"
            append_text_with_retry(target_daily_note_path, callout)

        return {
            "status": "ok",
            "path": str(destination),
            "source": transcript_source,
            "mode": normalized_mode,
        }

    def append_url_to_daily_note(self, url: str) -> dict[str, str]:
        cleaned_url = (url or "").strip()
        if not cleaned_url:
            raise ValueError("Missing required field: url")

        daily_note_path = self.vault_root / f"{date.today().isoformat()}.md"
        daily_note_path.parent.mkdir(parents=True, exist_ok=True)
        existing_content = (
            read_text_with_retry(daily_note_path)
            if daily_note_path.exists()
            else ""
        )
        cleaned_existing = existing_content.rstrip("\n")
        write_text_with_retry(
            daily_note_path,
            f"{cleaned_existing}\n{cleaned_url}\n",
        )
        return {"status": "ok"}

    def _fetch_from_transcript_lol(self, url: str, title: str, source: str) -> str:
        if self.client is None:
            self.client = TranscriptClient(load_env())
            self.client.authenticate()
        recording_id = self.client.find_recording_by_url(url)
        if recording_id:
            print(f"[transcribe] reusing existing recording {recording_id}")
        else:
            recording_id = self.client.create_recording(
                url=url,
                title=title,
                language="en",
                media_type=detect_media_type(source),
                source=source,
            )
        return wait_for_transcript(
            self.client,
            recording_id,
            "text",
            FALLBACK_TIMEOUT_SECONDS,
        )


class Handler(BaseHTTPRequestHandler):
    service: TranscriptService

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path not in {"/transcript", "/daily-note"}:
            self._send_json(404, {"status": "error", "message": "Not Found"})
            return

        try:
            body = self._read_json_body()
            url = str(body.get("url", "")).strip()
            if self.path == "/daily-note":
                self._send_json(200, self.service.append_url_to_daily_note(url))
                return

            title = body.get("title")
            description = body.get("description", "")
            ai_summary = body.get("ai_summary", "")
            mode = body.get("mode", "full")
            result = self.service.save_from_url(
                url,
                title if isinstance(title, str) else None,
                description if isinstance(description, str) else "",
                ai_summary if isinstance(ai_summary, str) else "",
                mode if isinstance(mode, str) else "full",
            )
            self._send_json(200, result)
        except ValueError as exc:
            self._send_json(400, {"status": "error", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"status": "error", "message": str(exc)})

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            raise ValueError("Missing request body")
        try:
            content_len = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        payload = self.rfile.read(content_len)
        try:
            body = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[transcript_server] {self.address_string()} - {fmt % args}")


def main() -> None:
    service = TranscriptService(DEFAULT_OUTPUT_DIR)
    Handler.service = service
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[transcript_server] listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
