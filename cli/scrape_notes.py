#!/usr/bin/env python3
"""
scrape_notes.py - Archive date-named Obsidian vault root notes into z.Ingestion/.

This script looks for notes in the vault root that match exactly:
  YYYY-MM-DD.md

It extracts:
  - Plain text + URLs (kept as-is)
  - Local image embeds: ![[image.png]] (OCR via Claude Vision)
  - Remote image embeds: ![](https://...) or ![alt](https://...) (download + OCR)

Then it writes a transcript markdown file into:
  <vault-root>/z.Ingestion/<sanitized-title>.md

And moves the source note into:
  <vault-root>/processed/<original-name>.md (unique if needed)

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/scrape_notes.py --dry-run
  python3 /Users/leon/Documents/Code/vault-orchestrator/cli/scrape_notes.py

No external dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as date_type
from pathlib import Path

from archive_youtube import fetch_youtube_metadata
from daily_note_youtube import YOUTUBE_URL_RE
from export_transcripts import DEFAULT_OUTPUT_DIR, ensure_daily_note_link, sanitize_title
from export_transcripts import extract_youtube_id
from transcript_server import TranscriptService


SWIFT_OCR_SCRIPT = Path(__file__).resolve().parent / "ocr_vision.swift"

DATE_ONLY_NOTE_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})\.md$")
LOCAL_IMAGE_RE = re.compile(r"!\[\[(?P<target>[^\]]+?)\]\]")
REMOTE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)")

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive YYYY-MM-DD.md notes in the Obsidian vault root into z.Ingestion/ with OCR.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be processed without writing or moving files.",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_OUTPUT_DIR.parent,
        help=f"Obsidian vault root (default: {DEFAULT_OUTPUT_DIR.parent})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write transcript markdown files into (default: <vault-root>/z.Ingestion)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite transcript files even if they already exist.",
    )
    return parser.parse_args()



def read_text_with_retry(path: Path, attempts: int = 10, delay_s: float = 0.5) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last_exc = exc
            time.sleep(delay_s)
    raise last_exc or OSError(f"Unable to read {path}")


def read_bytes_with_retry(path: Path, attempts: int = 10, delay_s: float = 0.5) -> bytes:
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return path.read_bytes()
        except OSError as exc:
            last_exc = exc
            time.sleep(delay_s)
    raise last_exc or OSError(f"Unable to read {path}")


def is_transient_lock_error(exc: Exception) -> bool:
    if isinstance(exc, OSError):
        return getattr(exc, "errno", None) == 11
    return False


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



def find_date_only_files(vault_root: Path) -> list[Path]:
    matches: list[Path] = []
    for path in sorted(vault_root.glob("*.md")):
        if not path.is_file():
            continue
        if DATE_ONLY_NOTE_RE.fullmatch(path.name):
            matches.append(path)
    return matches


def extract_local_images(content: str, vault_root: Path) -> list[Path]:
    paths: list[Path] = []
    for match in LOCAL_IMAGE_RE.finditer(content):
        raw_target = match.group("target").strip()
        if not raw_target:
            continue
        target = raw_target.split("|", 1)[0].strip()
        if not target:
            continue
        candidate = (vault_root / target).expanduser()
        paths.append(candidate)
    return paths


def extract_remote_images(content: str) -> list[str]:
    urls: list[str] = []
    for match in REMOTE_IMAGE_RE.finditer(content):
        url = match.group("url").strip()
        if not url:
            continue
        urls.append(url)
    return urls


def extract_bare_youtube_urls(content: str) -> tuple[list[str], str]:
    urls: list[str] = []
    remaining_lines: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            match = YOUTUBE_URL_RE.fullmatch(stripped)
            if match:
                urls.append(match.group(0))
                continue
        remaining_lines.append(line)

    remaining_content = "\n".join(remaining_lines).strip()
    return urls, remaining_content


def ocr_image_file(image_path: Path) -> str:
    """Use macOS Vision framework via Swift subprocess for local OCR."""
    result = subprocess.run(
        ["swift", str(SWIFT_OCR_SCRIPT), str(image_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Swift OCR failed")
    text = result.stdout.strip()
    return text or "NO_TEXT"


def ocr_local_image(path: Path) -> str:
    # iCloud/Obsidian can briefly lock files; retry on transient errors.
    read_bytes_with_retry(path, attempts=10, delay_s=0.5)  # ensure readable
    return ocr_image_file(path)


def download_to_temp(url: str, timeout_s: int = 60) -> Path:
    req = urllib.request.Request(
        url,
        headers={"user-agent": "vault-orchestrator/1.0 (+scrape_notes.py)"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(resp.read())
        tmp.close()
        return Path(tmp.name)


def ocr_remote_image(url: str) -> str:
    tmp_path = download_to_temp(url)
    try:
        return ocr_image_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def unique_processed_path(processed_dir: Path, original_name: str) -> Path:
    candidate = processed_dir / original_name
    if not candidate.exists():
        return candidate

    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    index = 1
    while True:
        candidate = processed_dir / f"{stem} {index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def build_note_markdown(
    note_title: str,
    note_date: str,
    original_content: str,
    ocr_results: list[tuple[str, str]],
) -> str:
    sections: list[str] = [
        f"# {note_title}",
        "",
        f"#[[{note_date}]]",
        "",
        "**Source:** Obsidian vault note",
        f"**Date:** {note_date}",
        "",
        "## Original Content",
        original_content.rstrip(),
        "",
    ]

    if ocr_results:
        sections.append("## Image Text (OCR)")
        for label, text in ocr_results:
            sections.extend(
                [
                    f"### {label}",
                    text.rstrip(),
                    "",
                ]
            )

    sections.append("")
    return "\n".join(sections).lstrip()


def extract_note_date(path: Path) -> str:
    match = DATE_ONLY_NOTE_RE.fullmatch(path.name)
    if not match:
        return date_type.today().isoformat()
    return match.group("day")


def main() -> int:
    args = parse_args()
    vault_root = args.vault_root.expanduser()
    output_dir = args.output_dir.expanduser() if args.output_dir else vault_root / "z.Ingestion"
    processed_dir = vault_root / "processed"

    date_files = find_date_only_files(vault_root)
    print(f"[scrape] found {len(date_files)} date-only file(s) in {vault_root}")
    if not date_files:
        return 0

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    moved = 0
    skipped_existing = 0
    failures = 0
    ocr_errors = 0
    youtube_processed = 0
    transcript_service: TranscriptService | None = None

    for note_path in date_files:
        note_date = extract_note_date(note_path)
        note_title = note_path.stem
        safe_title = sanitize_title(note_title)
        destination = output_dir / f"{safe_title}.md"

        try:
            content = read_text_with_retry(note_path).strip()
            youtube_urls, remaining_content = extract_bare_youtube_urls(content)

            if youtube_urls:
                if args.dry_run:
                    for url in youtube_urls:
                        print(f"[scrape] would process YouTube URL from {note_path.name}: {url}")
                else:
                    if transcript_service is None:
                        transcript_service = TranscriptService(output_dir)
                    for url in youtube_urls:
                        video_id = extract_youtube_id(url)
                        if not video_id:
                            raise RuntimeError(f"Invalid YouTube URL: {url}")
                        metadata = fetch_youtube_metadata(video_id)
                        transcript_service.save_from_url(
                            url=url,
                            title=metadata["title"],
                            description=metadata["description"],
                            mode="full",
                            daily_note_path=vault_root / "Daily Notes" / f"{note_date}.md",
                        )
                        youtube_processed += 1
                        print(f"[scrape] processed YouTube URL from {note_path.name}: {metadata['title']}")

            if not remaining_content:
                if args.dry_run:
                    print(f"[scrape] would skip date ingest for {note_path.name}; YouTube URL(s) only")
                    continue

                processed_path = unique_processed_path(processed_dir, note_path.name)
                shutil.move(str(note_path), str(processed_path))
                moved += 1
                print(f"[scrape] moved {note_path.name} -> processed/{processed_path.name}")
                continue

            if destination.exists() and not args.force:
                skipped_existing += 1
                print(f"[scrape] skip existing {destination.name}")
                continue

            if args.dry_run:
                print(f"[scrape] would write {destination.name} from {note_path.name}")
                continue

            local_images = extract_local_images(remaining_content, vault_root)
            remote_images = extract_remote_images(remaining_content)

            ocr_results: list[tuple[str, str]] = []
            had_transient_error = False

            for image_path in local_images:
                label = image_path.name
                if not image_path.exists():
                    ocr_results.append((label, f"ERROR: missing local image at {image_path}"))
                    continue
                try:
                    text = ocr_local_image(image_path)
                    ocr_results.append((label, text))
                except Exception as exc:
                    if is_transient_lock_error(exc):
                        had_transient_error = True
                    ocr_errors += 1
                    ocr_results.append((label, f"ERROR: OCR failed: {exc}"))

            for url in remote_images:
                label = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or url
                try:
                    text = ocr_remote_image(url)
                    ocr_results.append((label, text))
                except Exception as exc:
                    ocr_errors += 1
                    ocr_results.append((label, f"ERROR: download/OCR failed: {exc}"))

            if had_transient_error:
                failures += 1
                print(
                    f"[scrape] transient error; leaving {note_path.name} in place for retry",
                    file=sys.stderr,
                )
                continue

            write_text_with_retry(
                destination,
                build_note_markdown(note_title, note_date, remaining_content, ocr_results),
            )

            daily_note_path = vault_root / "Daily Notes" / f"{note_date}.md"
            ensure_daily_note_link(daily_note_path, safe_title)
            written += 1
            print(f"[scrape] wrote {destination}")

            processed_path = unique_processed_path(processed_dir, note_path.name)
            shutil.move(str(note_path), str(processed_path))
            moved += 1
            print(f"[scrape] moved {note_path.name} -> processed/{processed_path.name}")
        except Exception as exc:
            failures += 1
            print(f"[scrape] ERROR {note_path.name}: {exc}", file=sys.stderr)

    print(
        "[scrape] summary: "
        f"written={written} moved={moved} skipped_existing={skipped_existing} "
        f"youtube_processed={youtube_processed} ocr_errors={ocr_errors} failures={failures}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[scrape] interrupted", file=sys.stderr)
        raise SystemExit(130)
