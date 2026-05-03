#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

VAULT_DEFAULT = (
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator"
)

DATE_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")
TRANSCRIPT_DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")


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


def _next_duplicate_archive_path(processed_dir: Path, date: str) -> Path:
    idx = 1
    while True:
        candidate = processed_dir / f"{date}-dup{idx}.md"
        if not candidate.exists():
            return candidate
        idx += 1


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
            summary.warnings += 1
            print(f"[WARN recover target exists] {dest_path}")
            continue
        if verbose:
            print(f"[RECOVER rename] {source_path.name} -> {dest_path.name}")
        if apply:
            source_path.rename(dest_path)
        summary.recovered_transcript_renamed += 1

        daily_note_path = daily_notes_dir / f"{date}.md"
        if not daily_note_path.exists():
            continue
        old_link = f"[[Transcripts/{date}]]"
        new_link = f"[[{date} ingest]]"
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
) -> None:
    m = DATE_FILENAME_RE.match(source_path.name)
    if not m:
        return
    date = m.group("date")

    transcript_dest = transcripts_dir / f"{date} ingest.md"
    archive_dest = processed_dir / source_path.name
    daily_note_path = daily_notes_dir / source_path.name
    link_fragment = f"[[{date} ingest]]"

    if not transcripts_dir.exists():
        summary.warnings += 1
        print(f"[WARN] Missing directory: {transcripts_dir}")
    if not processed_dir.exists():
        summary.warnings += 1
        print(f"[WARN] Missing directory: {processed_dir}")

    # 1) Copy to Transcripts/YYYY-MM-DD ingest.md (skip if exists)
    if transcript_dest.exists():
        summary.transcript_skipped_exists += 1
        if verbose:
            print(f"[SKIP transcript exists] {transcript_dest}")
    else:
        summary.transcript_created += 1
        if verbose:
            print(f"[COPY] {source_path.name} -> {transcript_dest}")
        if apply:
            transcript_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, transcript_dest)

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
            "Transcripts/YYYY-MM-DD ingest.md, archiving originals to processed/, "
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
            "Scan Transcripts/ for YYYY-MM-DD.md files, rename them to "
            "YYYY-MM-DD ingest.md, and fix matching daily note links."
        ),
    )
    args = parser.parse_args()

    vault_dir = Path(args.vault_dir).expanduser().resolve()
    transcripts_dir = vault_dir / "Transcripts"
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
    if summary.warnings:
        print(f"- Warnings: {summary.warnings}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
