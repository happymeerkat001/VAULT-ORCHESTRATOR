#!/usr/bin/env python3
"""Shared YouTube summary scraping helpers for transcript/archive workflows."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request


def extract_initial_data(html: str) -> dict:
    markers = (
        "var ytInitialData = ",
        "window['ytInitialData'] = ",
        'window["ytInitialData"] = ',
    )
    decoder = json.JSONDecoder()
    for marker in markers:
        marker_index = html.find(marker)
        if marker_index == -1:
            continue
        start = html.find("{", marker_index + len(marker))
        if start == -1:
            continue
        try:
            payload, _ = decoder.raw_decode(html[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def walk_json(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_json(item)


def extract_text_content(node: object) -> str:
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        parts = [extract_text_content(item) for item in node]
        return " ".join(part for part in parts if part).strip()
    if not isinstance(node, dict):
        return ""

    simple_text = node.get("simpleText")
    if isinstance(simple_text, str) and simple_text.strip():
        return simple_text.strip()

    runs = node.get("runs")
    if isinstance(runs, list):
        parts = [extract_text_content(item) for item in runs]
        return " ".join(part for part in parts if part).strip()

    text = node.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    segments = node.get("content")
    if segments is not None:
        content_text = extract_text_content(segments)
        if content_text:
            return content_text

    return ""


def find_video_summary_text(data: dict) -> str:
    candidate_keys = (
        "summary",
        "content",
        "snippet",
        "description",
        "attributedSummaryBodyText",
        "summaryText",
    )
    for node in walk_json(data):
        for key, value in node.items():
            if "videoSummary" not in key:
                continue
            renderer = value if isinstance(value, dict) else node
            for candidate_key in candidate_keys:
                summary_text = extract_text_content(renderer.get(candidate_key))
                if summary_text:
                    return re.sub(r"\s+", " ", summary_text).strip()

            descendant_texts: list[str] = []
            for descendant in walk_json(renderer):
                for candidate_key in candidate_keys:
                    summary_text = extract_text_content(descendant.get(candidate_key))
                    if summary_text:
                        descendant_texts.append(re.sub(r"\s+", " ", summary_text).strip())
            if descendant_texts:
                return max(descendant_texts, key=len)
    return ""


def fetch_youtube_ai_summary(video_id: str) -> str:
    if not video_id:
        return ""

    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    request = urllib.request.Request(
        watch_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    initial_data = extract_initial_data(html)
    if not initial_data:
        return ""

    return find_video_summary_text(initial_data)
