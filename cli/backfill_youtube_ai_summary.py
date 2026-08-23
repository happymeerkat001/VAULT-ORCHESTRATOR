#!/usr/bin/env python3
"""
backfill_youtube_ai_summary.py - Upgrade saved YouTube notes in z.Ingestion/ with
an AI Summary block generated from Transcript.lol first.

This keeps existing filenames and transcript bodies intact. It only adds or
refreshes the AI Summary section in place for notes that are missing it.

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/backfill_youtube_ai_summary.py --dry-run
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/backfill_youtube_ai_summary.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from export_transcripts import DEFAULT_OUTPUT_DIR, extract_youtube_id, read_note_metadata
from scrape_notes import read_text_with_retry, write_text_with_retry
from transcript_lol_summary import prepare_youtube_summary_context
from transcribe import load_env


AI_SUMMARY_BLOCK_RE = re.compile(
    r"^#{1,6}\s+AI Summary\s*\n\n.*?(?=\n(?:---|#{1,6}\s)|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill AI Summary blocks into saved YouTube notes in z.Ingestion/."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List notes that would be upgraded without writing files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to scan for notes (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args()


def note_needs_ai_summary(content: str) -> bool:
    return AI_SUMMARY_BLOCK_RE.search(content) is None


def inject_ai_summary(content: str, ai_summary: str) -> str:
    summary_text = ai_summary.strip()
    if not summary_text:
        return content

    block = f"## AI Summary\n\n{summary_text}\n\n"

    if AI_SUMMARY_BLOCK_RE.search(content):
        return AI_SUMMARY_BLOCK_RE.sub(block.rstrip("\n"), content, count=1)

    divider = "\n---\n\n"
    if divider in content:
        return content.replace(divider, f"\n\n{block}---\n\n", 1)

    return content.rstrip() + "\n\n" + block


def iter_youtube_notes(output_dir: Path) -> list[Path]:
    return sorted(path for path in output_dir.glob("*.md") if path.is_file())


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser()
    env = load_env()
    client = None

    if not output_dir.exists():
        print(f"[backfill-ai-summary] missing output dir: {output_dir}", file=sys.stderr)
        return 1

    processed = 0
    updated = 0
    skipped = 0
    failures = 0

    for note_path in iter_youtube_notes(output_dir):
        try:
            content = read_text_with_retry(note_path)
            if not note_needs_ai_summary(content):
                skipped += 1
                continue

            url, title, _description = read_note_metadata(note_path)
            if not url or not extract_youtube_id(url):
                skipped += 1
                continue

            if args.dry_run:
                processed += 1
                print(f"[backfill-ai-summary] would upgrade {note_path.name}")
                continue

            summary_context = prepare_youtube_summary_context(
                url,
                title or note_path.stem,
                env=env,
                client=client,
                timeout_seconds=600,
            )
            client = summary_context.client or client
            summary = summary_context.summary.strip()
            if not summary:
                failures += 1
                reason = summary_context.summary_failure or "Transcript.lol summary unavailable"
                print(f"[backfill-ai-summary] failed {note_path.name}: {reason}", file=sys.stderr)
                continue

            updated_content = inject_ai_summary(content, summary)
            if updated_content == content:
                skipped += 1
                continue

            write_text_with_retry(note_path, updated_content)
            processed += 1
            updated += 1
            print(f"[backfill-ai-summary] upgraded {note_path.name}")
        except Exception as exc:
            failures += 1
            print(f"[backfill-ai-summary] ERROR {note_path.name}: {exc}", file=sys.stderr)

    print(
        "[backfill-ai-summary] summary: "
        f"processed={processed} updated={updated} skipped={skipped} failures={failures}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[backfill-ai-summary] interrupted", file=sys.stderr)
        raise SystemExit(130)
