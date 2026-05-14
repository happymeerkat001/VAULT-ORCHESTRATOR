#!/usr/bin/env python3
"""Transcript.lol-backed summary helpers for YouTube ingests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from export_transcripts import extract_youtube_id
from transcribe import TranscriptClient, detect_media_type, detect_source, load_env, wait_for_recording_terminal
from youtube_summary import fetch_youtube_ai_summary

SUMMARY_PROMPT_ID_ENV = "TRANSCRIPT_LOL_SUMMARY_PROMPT_ID"
SUMMARY_TWEAK_ENV = "TRANSCRIPT_LOL_SUMMARY_TWEAK"
DEFAULT_SUMMARY_TWEAK = (
    "Write a detailed, Obsidian-ready summary with the main ideas, concrete takeaways, "
    "and any specific examples or arguments worth preserving. Avoid one-sentence summaries."
)
DEFAULT_TIMEOUT_SECONDS = 600
INSIGHT_POLL_INTERVAL = 5
INSIGHT_POLL_TIMEOUT = 120


@dataclass(frozen=True)
class YoutubeSummaryContext:
    client: TranscriptClient | None
    recording_id: str | None
    summary: str


def _coalesce_string(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _collect_insights(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("insights", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _extract_insight_content(insight: dict[str, Any], prompt_id: str) -> str:
    prompt = insight.get("prompt")
    if isinstance(prompt, dict):
        prompt_match = _coalesce_string(prompt, "id")
        if prompt_match and prompt_match != prompt_id:
            return ""
    else:
        prompt_match = _coalesce_string(insight, "promptId", "prompt_id")
        if prompt_match and prompt_match != prompt_id:
            return ""

    content = insight.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return ""


def _poll_for_insight_content(
    client: TranscriptClient,
    recording_id: str,
    prompt_id: str,
) -> str:
    """Poll list_insights until content for prompt_id appears or timeout."""
    deadline = time.time() + INSIGHT_POLL_TIMEOUT
    while time.time() < deadline:
        try:
            insights = _collect_insights(client.list_insights(recording_id))
        except Exception:
            insights = []
        for insight in insights:
            content = _extract_insight_content(insight, prompt_id)
            if content:
                return content
        time.sleep(INSIGHT_POLL_INTERVAL)
    return ""


def get_or_create_summary(
    client: TranscriptClient,
    recording_id: str,
    prompt_id: str,
    tweak_query: str = "",
) -> str:
    prompt_id = prompt_id.strip()
    if not prompt_id:
        return ""

    # Check for existing insight with content
    try:
        insights = _collect_insights(client.list_insights(recording_id))
    except Exception:
        insights = []

    for insight in insights:
        content = _extract_insight_content(insight, prompt_id)
        if content:
            return content

    # Create insight — returns QUEUED with empty content
    try:
        client.create_insight(recording_id, prompt_id, tweak_query=tweak_query)
    except Exception:
        return ""

    # Poll until content is generated
    return _poll_for_insight_content(client, recording_id, prompt_id)


def _get_summary_prompt_config(env: dict[str, str]) -> tuple[str, str]:
    prompt_id = env.get(SUMMARY_PROMPT_ID_ENV, "").strip()
    tweak_query = env.get(SUMMARY_TWEAK_ENV, "").strip() or DEFAULT_SUMMARY_TWEAK
    return prompt_id, tweak_query


def prepare_youtube_summary_context(
    url: str,
    title: str,
    env: dict[str, str] | None = None,
    client: TranscriptClient | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> YoutubeSummaryContext:
    cleaned_url = (url or "").strip()
    video_id = extract_youtube_id(cleaned_url)
    if not video_id:
        return YoutubeSummaryContext(client=None, recording_id=None, summary="")

    env = env or load_env()
    prompt_id, tweak_query = _get_summary_prompt_config(env)

    transcript_client = client
    recording_id: str | None = None
    summary = ""

    try:
        source = detect_source(cleaned_url)
        if transcript_client is None:
            transcript_client = TranscriptClient(env)
            transcript_client.authenticate()

        recording_id = transcript_client.find_recording_by_url(cleaned_url)
        if recording_id:
            print(f"[transcribe] reusing existing recording {recording_id}")
        else:
            recording_id = transcript_client.create_recording(
                url=cleaned_url,
                title=title,
                language="en",
                media_type=detect_media_type(source),
                source=source,
                external_id=f"youtube:{video_id}",
            )
            wait_for_recording_terminal(transcript_client, recording_id, timeout_seconds)

        if prompt_id:
            summary = get_or_create_summary(
                transcript_client,
                recording_id,
                prompt_id,
                tweak_query=tweak_query,
            )
    except Exception:
        recording_id = recording_id or None

    if not summary:
        summary = fetch_youtube_ai_summary(video_id)

    return YoutubeSummaryContext(
        client=transcript_client,
        recording_id=recording_id,
        summary=summary,
    )
