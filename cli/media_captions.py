#!/usr/bin/env python3
"""Shared yt-dlp-backed caption and metadata helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from html import unescape
from pathlib import Path
from typing import Any


_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}\.\d{3}"
)
_CUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def fetch_yt_dlp_metadata(url: str) -> dict[str, Any] | None:
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        return None

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        cleaned_url,
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def fetch_vimeo_captions(url: str, language: str = "en") -> str | None:
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        return None

    requested_language = (language or "en").strip() or "en"
    language_order = _build_language_order(requested_language)

    with tempfile.TemporaryDirectory(prefix="vimeo-captions-") as temp_dir:
        output_template = str(Path(temp_dir) / "captions.%(ext)s")
        for candidate_language in language_order:
            for existing_file in Path(temp_dir).glob("*.vtt"):
                existing_file.unlink(missing_ok=True)
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--write-subs",
                "--sub-format",
                "vtt",
                "--sub-langs",
                candidate_language,
                "--output",
                output_template,
                cleaned_url,
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                continue

            subtitle_files = sorted(Path(temp_dir).glob("*.vtt"))
            for subtitle_file in subtitle_files:
                transcript_text = parse_webvtt(subtitle_file.read_text(encoding="utf-8", errors="replace"))
                if transcript_text:
                    return transcript_text

    return None


def parse_webvtt(raw_text: str) -> str | None:
    lines = raw_text.splitlines()
    blocks: list[str] = []
    current_lines: list[str] = []
    in_note_block = False

    for raw_line in lines:
        line = raw_line.strip("\ufeff").strip()
        if not line:
            if current_lines:
                block_text = _normalize_block_lines(current_lines)
                if block_text:
                    blocks.append(block_text)
                current_lines = []
            in_note_block = False
            continue

        upper_line = line.upper()
        if upper_line == "WEBVTT":
            continue
        if upper_line.startswith("NOTE"):
            in_note_block = True
            continue
        if in_note_block:
            continue
        if upper_line.startswith(("STYLE", "REGION")):
            continue
        if _TIMESTAMP_LINE_RE.match(line):
            continue
        if _CUE_ID_RE.match(line) and not current_lines:
            continue

        current_lines.append(line)

    if current_lines:
        block_text = _normalize_block_lines(current_lines)
        if block_text:
            blocks.append(block_text)

    deduped_blocks: list[str] = []
    for block in blocks:
        if not deduped_blocks or deduped_blocks[-1] != block:
            deduped_blocks.append(block)
    return "\n".join(deduped_blocks).strip() or None


def _build_language_order(language: str) -> list[str]:
    normalized = language.lower()
    if normalized == "en":
        return ["en", "en.*", "all"]
    if normalized.startswith("en-"):
        return [normalized, "en", "en.*", "all"]

    order = [normalized]
    if "-" in normalized:
        base_language = normalized.split("-", 1)[0]
        if base_language not in order:
            order.append(base_language)
    return order


def _normalize_block_lines(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = _HTML_TAG_RE.sub("", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
