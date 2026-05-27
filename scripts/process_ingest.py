#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

VAULT_DEFAULT = (
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault"
)

DATE_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")
TRANSCRIPT_DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")
EMBED_RE = re.compile(r"!\[\[([^\]]+\.(?:png|jpe?g))\]\]", re.IGNORECASE)
YOUTUBE_ONLY_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s)>\]]+|youtu\.be/[^\s)>\]]+)"
)


@dataclass
class Summary:
    scanned: int = 0
    candidates: int = 0

    transcript_created: int = 0
    transcript_skipped_exists: int = 0

    archived: int = 0
    archive_skipped_exists: int = 0
    archive_skipped_missing_source: int = 0
    archive_duplicated: int = 0

    link_appended: int = 0
    link_skipped_exists: int = 0
    link_skipped_missing_daily_note: int = 0

    warnings: int = 0
    recovered_transcript_renamed: int = 0
    recovered_links_fixed: int = 0
    imgur_uploaded: int = 0
    imgur_skipped_missing: int = 0
    imgur_failed: int = 0
    images_deleted: int = 0


def _iter_root_date_files(vault_dir: Path) -> list[Path]:
    paths = []
    for path in vault_dir.glob("*.md"):
        if not path.is_file():
            continue
        if DATE_FILENAME_RE.match(path.name):
            paths.append(path)
    return sorted(paths)


def _file_contains_line_fragment(path: Path, fragment: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(fragment in line for line in text.splitlines())


def _append_line(path: Path, line: str, *, apply: bool) -> bool:
    existing = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    if existing:
        if not existing[-1].endswith("\n"):
            existing[-1] += "\n"
        existing.append(line + "\n")
    else:
        existing = [line + "\n"]
    if apply:
        path.write_text("".join(existing), encoding="utf-8")
    return True


def _describe_action(apply: bool) -> str:
    return "APPLY" if apply else "DRY-RUN"


def _is_youtube_url_only(content: str) -> bool:
    """Return True if every non-empty line is a bare YouTube URL."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return bool(lines) and all(YOUTUBE_ONLY_RE.fullmatch(line) for line in lines)


def _next_duplicate_archive_path(processed_dir: Path, date: str) -> Path:
    idx = 1
    while True:
        candidate = processed_dir / f"{date} source-dup{idx}.md"
        if not candidate.exists():
            return candidate
        idx += 1


def _load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _resolve_embed_image_path(vault_dir: Path, embed_ref: str) -> Path | None:
    embed_path = Path(embed_ref)
    candidates = [
        vault_dir / embed_ref,
        vault_dir / embed_path.name,
        vault_dir / "Attachments" / embed_path.name,
        vault_dir / "Attachments" / "Scans" / embed_path.name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _upload_image_to_imgur(image_path: Path, client_id: str) -> str | None:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = urllib.parse.urlencode({"image": image_b64, "type": "base64"}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.imgur.com/3/image",
        data=payload,
        headers={"Authorization": f"Client-ID {client_id}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("success"):
        return None
    data = body.get("data", {})
    link = data.get("link")
    if not isinstance(link, str) or not link:
        return None
    return link


def upload_images_to_imgur(
    md_text: str,
    *,
    vault_dir: Path,
    client_id: str,
    verbose: bool,
    summary: Summary,
) -> str:
    if not client_id:
        return md_text

    def _replace(match: re.Match[str]) -> str:
        embed_ref = match.group(1).strip()
        image_path = _resolve_embed_image_path(vault_dir, embed_ref)
        if image_path is None:
            summary.imgur_skipped_missing += 1
            if verbose:
                print(f"[IMGUR skip missing] {embed_ref}")
            return match.group(0)
        try:
            link = _upload_image_to_imgur(image_path, client_id)
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            summary.imgur_failed += 1
            if verbose:
                print(f"[IMGUR fail] {embed_ref}")
            return match.group(0)
        if not link:
            summary.imgur_failed += 1
            if verbose:
                print(f"[IMGUR fail] {embed_ref}")
            return match.group(0)
        summary.imgur_uploaded += 1
        if verbose:
            print(f"[IMGUR ok] {embed_ref} -> {link}")
        return f"![]({link})"

    return EMBED_RE.sub(_replace, md_text)


def _recover_transcripts_and_links(
    *,
    transcripts_dir: Path,
    daily_notes_dir: Path,
    apply: bool,
    verbose: bool,
    summary: Summary,
) -> None:
    if not transcripts_dir.exists():
        if verbose:
            print(f"[SKIP recover missing dir] {transcripts_dir}")
        return
    for source_path in sorted(transcripts_dir.glob("*.md")):
        if not source_path.is_file():
            continue
        m = TRANSCRIPT_DATE_RE.match(source_path.name)
        if not m:
            continue
        date = m.group("date")
        dest_path = transcripts_dir / f"{date} ingest.md"
        if dest_path.exists():
            idx = 2
            while True:
                candidate = transcripts_dir / f"{date} ingest {idx}.md"
                if not candidate.exists():
                    dest_path = candidate
                    break
                idx += 1
        if verbose:
            print(f"[RECOVER rename] {source_path.name} -> {dest_path.name}")
        if apply:
            source_path.rename(dest_path)
        summary.recovered_transcript_renamed += 1

        daily_note_path = daily_notes_dir / f"{date}.md"
        if not daily_note_path.exists():
            continue
        old_link = f"[[z.Ingestion/{date}]]"
        new_link = f"[[{dest_path.stem}]]"
        text = daily_note_path.read_text(encoding="utf-8", errors="ignore")
        if old_link not in text:
            continue
        if verbose:
            print(f"[RECOVER link] {daily_note_path} :: {old_link} -> {new_link}")
        if apply:
            daily_note_path.write_text(text.replace(old_link, new_link), encoding="utf-8")
        summary.recovered_links_fixed += 1


def process_one(
    source_path: Path,
    *,
    transcripts_dir: Path,
    processed_dir: Path,
    daily_notes_dir: Path,
    apply: bool,
    verbose: bool,
    summary: Summary,
    vault_dir: Path,
    imgur_client_id: str,
) -> None:
    m = DATE_FILENAME_RE.match(source_path.name)
    if not m:
        return
    date = m.group("date")
    content = source_path.read_text(encoding="utf-8", errors="ignore").strip()

    if _is_youtube_url_only(content):
        if verbose:
            print(f"[SKIP youtube-only] {source_path.name} -> handled by scrape_notes.py")
        return

    transcript_dest = transcripts_dir / f"*{date} ingest.md"
    if transcript_dest.exists():
        idx = 2
        while True:
            candidate = transcripts_dir / f"*{date} ingest {idx}.md"
            if not candidate.exists():
                transcript_dest = candidate
                break
            idx += 1
    archive_dest = processed_dir / f"{date} source.md"
    daily_note_path = daily_notes_dir / source_path.name
    link_fragment = f"[[{transcript_dest.stem}]]"

    if not transcripts_dir.exists():
        summary.warnings += 1
        print(f"[WARN] Missing directory: {transcripts_dir}")
    if not processed_dir.exists():
        summary.warnings += 1
        print(f"[WARN] Missing directory: {processed_dir}")

    # 1) Copy to z.Ingestion/*YYYY-MM-DD ingest[ N].md
    summary.transcript_created += 1
    if verbose:
        print(f"[COPY] {source_path.name} -> {transcript_dest}")
    if apply:
            transcript_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, transcript_dest)
            if imgur_client_id:
                transcript_text = transcript_dest.read_text(encoding="utf-8", errors="ignore")
                replaced_text = upload_images_to_imgur(
                    transcript_text,
                    vault_dir=vault_dir,
                    client_id=imgur_client_id,
                    verbose=verbose,
                    summary=summary,
                )
                if replaced_text != transcript_text:
                    transcript_dest.write_text(replaced_text, encoding="utf-8")
            # 4) Delete all image files from vault root (post-transfer + post-imgur)
            _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".gif", ".tiff", ".tif", ".bmp"}
            for f in vault_dir.glob("*"):
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
                    summary.images_deleted += 1
                    if verbose:
                        print(f"[DELETE image] {f.name}")
                    f.unlink()

    # 2) Archive original to processed/ and always move source out of root
    if not source_path.exists():
        summary.archive_skipped_missing_source += 1
        if verbose:
            print(f"[SKIP missing source] {source_path}")
    else:
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        if archive_dest.exists():
            duplicate_dest = _next_duplicate_archive_path(processed_dir, date)
            summary.archived += 1
            summary.archive_duplicated += 1
            if verbose:
                print(f"[MOVE→DUP] {source_path.name} -> {duplicate_dest}")
            if apply:
                shutil.move(str(source_path), str(duplicate_dest))
        else:
            summary.archived += 1
            if verbose:
                print(f"[MOVE] {source_path.name} -> {archive_dest}")
            if apply:
                shutil.move(str(source_path), str(archive_dest))

    # 3) Append link to Daily Notes/YYYY-MM-DD.md (skip if present)
    if not daily_note_path.exists():
        summary.link_skipped_missing_daily_note += 1
        if verbose:
            print(f"[SKIP missing daily note] {daily_note_path}")
    else:
        if _file_contains_line_fragment(daily_note_path, link_fragment):
            summary.link_skipped_exists += 1
            if verbose:
                print(f"[SKIP link exists] {daily_note_path} :: {link_fragment}")
        else:
            summary.link_appended += 1
            if verbose:
                print(f"[APPEND link] {daily_note_path} :: {link_fragment}")
            _append_line(daily_note_path, link_fragment, apply=apply)



def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process YYYY-MM-DD.md files in the vault root by copying them to "
            "z.Ingestion/*YYYY-MM-DD ingest.md, archiving originals to processed/, "
            "and appending a [[YYYY-MM-DD ingest]] link to the matching Daily Notes entry."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/process_ingest.py --verbose\n"
            "  python3 scripts/process_ingest.py --apply --verbose\n"
            "  python3 scripts/process_ingest.py --vault-dir /path/to/vault --verbose\n"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (copy/move/write). Omit for dry-run.",
    )
    parser.add_argument(
        "--vault-dir",
        default=VAULT_DEFAULT,
        help=f"Path to vault root (default: {VAULT_DEFAULT}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file actions.",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help=(
            "Scan z.Ingestion/ for YYYY-MM-DD.md files, rename them to "
            "YYYY-MM-DD ingest.md, and fix matching daily note links."
        ),
    )
    args = parser.parse_args()
    imgur_client_id = _load_env().get("IMGUR_CLIENT_ID", "").strip()

    vault_dir = Path(args.vault_dir).expanduser().resolve()
    transcripts_dir = vault_dir / "z.Ingestion"
    processed_dir = vault_dir / "processed"
    daily_notes_dir = vault_dir / "Daily Notes"

    md_files = list(vault_dir.glob("*.md"))
    summary = Summary(scanned=len(md_files))
    candidates = _iter_root_date_files(vault_dir)
    summary.candidates = len(candidates)

    if args.verbose:
        print(f"[{_describe_action(args.apply)}] vault={vault_dir}")
        print(f"[SCAN] root .md files={summary.scanned} date-files={summary.candidates}")

    for path in candidates:
        process_one(
            path,
            transcripts_dir=transcripts_dir,
            processed_dir=processed_dir,
            daily_notes_dir=daily_notes_dir,
            apply=args.apply,
            verbose=args.verbose,
            summary=summary,
            vault_dir=vault_dir,
            imgur_client_id=imgur_client_id,
        )

    if args.recover:
        _recover_transcripts_and_links(
            transcripts_dir=transcripts_dir,
            daily_notes_dir=daily_notes_dir,
            apply=args.apply,
            verbose=args.verbose,
            summary=summary,
        )

    print("")
    print("Summary")
    print(f"- Mode: {_describe_action(args.apply)}")
    print(f"- Scanned root .md: {summary.scanned}")
    print(f"- Candidates: {summary.candidates}")
    print(f"- Transcript created: {summary.transcript_created}")
    print(f"- Transcript skipped (exists): {summary.transcript_skipped_exists}")
    print(f"- Archived: {summary.archived}")
    print(f"- Archive duplicated path used: {summary.archive_duplicated}")
    print(f"- Archive skipped (missing source): {summary.archive_skipped_missing_source}")
    print(f"- Link appended: {summary.link_appended}")
    print(f"- Link skipped (exists): {summary.link_skipped_exists}")
    print(f"- Link skipped (missing daily note): {summary.link_skipped_missing_daily_note}")
    print(f"- Recovered transcript renames: {summary.recovered_transcript_renamed}")
    print(f"- Recovered daily-note links: {summary.recovered_links_fixed}")
    print(f"- Imgur uploaded: {summary.imgur_uploaded}")
    print(f"- Imgur skipped (missing local file): {summary.imgur_skipped_missing}")
    print(f"- Imgur failed: {summary.imgur_failed}")
    print(f"- Embedded images deleted from vault root: {summary.images_deleted}")
    if summary.warnings:
        print(f"- Warnings: {summary.warnings}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
