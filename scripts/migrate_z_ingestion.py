#!/usr/bin/env python3
"""Flatten and date-prefix an Obsidian z.Ingestion folder safely.

Default mode is a dry run. Pass --apply only after reviewing the report.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

KEEP_FOLDERS = ("People", "Official Docs", "Clippings", "read.done")
DATE_LINE_RE = re.compile(r"^\*\*Date:\*\*\s*(\d{4})-(\d{2})-(\d{2})\s*$", re.MULTILINE)
FRONTMATTER_DATE_RE = re.compile(
    r"^(published|created):\s*[\"']?(\d{4})-(\d{2})-(\d{2})", re.MULTILINE
)
FILENAME_DATE_RE = re.compile(
    r"^(?P<marker>\*)?(?P<year>\d{4})[-_ ]?(?P<month>\d{2})[-_ ]?(?P<day>\d{2})(?:\s+|$)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten z.Ingestion to four folders and prefix notes with YYYYMMDD."
    )
    parser.add_argument("--root", type=Path, required=True, help="Path to z.Ingestion.")
    parser.add_argument("--apply", action="store_true", help="Apply moves and link updates.")
    parser.add_argument(
        "--repair-links-from",
        type=Path,
        help="Repair direct wikilinks from an apply-run manifest after the moves finish.",
    )
    return parser.parse_args()


def date_prefix(path: Path) -> tuple[str, str]:
    if path.suffix.lower() != ".md":
        return "", "non-markdown"

    text = path.read_text(encoding="utf-8", errors="ignore")
    match = DATE_LINE_RE.search(text)
    if match:
        return "".join(match.groups()), "metadata:Date"

    match = FRONTMATTER_DATE_RE.search(text)
    if match:
        return "".join(match.groups()[1:]), f"frontmatter:{match.group(1)}"

    match = FILENAME_DATE_RE.match(path.stem)
    if match:
        return f"{match.group('year')}{match.group('month')}{match.group('day')}", "filename"

    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d"), "mtime"


def destination_folder(root: Path, path: Path) -> Path:
    relative = path.relative_to(root)
    if len(relative.parts) > 1 and relative.parts[0] in KEEP_FOLDERS:
        return Path(relative.parts[0])
    return Path()


def unique_destination(destination: Path, reserved: set[str]) -> tuple[Path, bool]:
    candidate = destination
    index = 2
    collided = False
    while str(candidate).casefold() in reserved:
        collided = True
        candidate = destination.with_name(f"{destination.stem} ({index}){destination.suffix}")
        index += 1
    reserved.add(str(candidate).casefold())
    return candidate, collided


def build_moves(root: Path) -> tuple[list[tuple[Path, Path, str]], Counter[str], int]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    moves: list[tuple[Path, Path, str]] = []
    reserved: set[str] = set()
    date_sources: Counter[str] = Counter()
    collisions = 0

    for source in files:
        folder = destination_folder(root, source)
        if source.suffix.lower() == ".md":
            prefix, date_source = date_prefix(source)
            date_sources[date_source] += 1
            stem = source.stem
            existing_date = FILENAME_DATE_RE.match(stem)
            if existing_date:
                marker = existing_date.group("marker") or ""
                stem = f"{prefix} {marker}{stem[existing_date.end():]}".rstrip()
            else:
                stem = f"{prefix} {stem}"
            destination = root / folder / f"{stem}{source.suffix}"
        else:
            destination = root / folder / source.name
            date_source = "non-markdown"

        destination, collided = unique_destination(destination, reserved)
        collisions += int(collided)
        moves.append((source, destination, date_source))

    return moves, date_sources, collisions


def link_replacements(root: Path, moves: list[tuple[Path, Path, str]]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for source, destination, _ in moves:
        if source.suffix.lower() != ".md" or source == destination:
            continue
        old = source.relative_to(root).with_suffix("").as_posix()
        new = destination.relative_to(root).with_suffix("").as_posix()
        replacements[f"[[z.Ingestion/{old}"] = f"[[z.Ingestion/{new}"
        if "/" in old:
            replacements[f"[[{old}"] = f"[[{new}"
    return replacements


def direct_link_replacements(root: Path, moves: list[tuple[Path, Path, str]]) -> dict[str, str]:
    """Update direct wikilinks only where the old z.Ingestion title was unique."""
    markdown_moves = [item for item in moves if item[0].suffix.lower() == ".md"]
    old_stem_counts = Counter(source.stem for source, _, _ in markdown_moves)
    replacements: dict[str, str] = {}
    for source, destination, _ in markdown_moves:
        if source == destination or old_stem_counts[source.stem] != 1:
            continue
        replacements[f"[[{source.stem}]]"] = f"[[{destination.stem}]]"
        replacements[f"[[{source.stem}|"] = f"[[{destination.stem}|"
    return replacements


def update_links(vault_root: Path, replacements: dict[str, str]) -> int:
    changed = 0
    ordered_replacements = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    for note in vault_root.rglob("*.md"):
        try:
            text = note.read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            continue
        updated = text
        for old, new in ordered_replacements:
            updated = updated.replace(old, new)
        if updated != text:
            try:
                note.write_text(updated, encoding="utf-8")
            except FileNotFoundError:
                continue
            changed += 1
    return changed


def remove_empty_directories(root: Path) -> int:
    removed = 0
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        if path.name in KEEP_FOLDERS:
            continue
        try:
            path.rmdir()
        except OSError:
            continue
        removed += 1
    return removed


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Missing z.Ingestion directory: {root}")

    vault_root = root.parent
    if args.repair_links_from:
        manifest = json.loads(args.repair_links_from.expanduser().read_text(encoding="utf-8"))
        moves = [
            (root / item["from"], root / item["to"], item["date_source"])
            for item in manifest["moves"]
        ]
        replacements = link_replacements(root, moves)
        replacements.update(direct_link_replacements(root, moves))
        changed_notes = update_links(vault_root, replacements)
        print("mode=REPAIR-LINKS")
        print(f"vault_notes_with_updated_links={changed_notes}")
        return 0

    moves, date_sources, collisions = build_moves(root)
    changed_moves = [(source, destination, kind) for source, destination, kind in moves if source != destination]
    replacements = link_replacements(root, moves)

    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"notes={sum(source.suffix.lower() == '.md' for source, _, _ in moves)}")
    print(f"files_to_move_or_rename={len(changed_moves)}")
    print("date_sources=" + ", ".join(f"{kind}:{count}" for kind, count in sorted(date_sources.items())))
    print(f"name_collision_suffixes={collisions}")
    for source, destination, kind in changed_moves[:20]:
        print(f"{source.relative_to(root)} -> {destination.relative_to(root)} [{kind}]")

    if not args.apply:
        return 0

    for folder in KEEP_FOLDERS:
        (root / folder).mkdir(exist_ok=True)

    manifest = {
        "root": str(root),
        "moves": [
            {
                "from": str(source.relative_to(root)),
                "to": str(destination.relative_to(root)),
                "date_source": kind,
            }
            for source, destination, kind in moves
        ],
    }
    manifest_path = vault_root / f".z-ingestion-migration-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    changed_notes = update_links(vault_root, replacements)
    for source, destination, _ in moves:
        if source == destination:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    removed_directories = remove_empty_directories(root)
    print(f"manifest={manifest_path}")
    print(f"vault_notes_with_updated_links={changed_notes}")
    print(f"removed_empty_directories={removed_directories}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
