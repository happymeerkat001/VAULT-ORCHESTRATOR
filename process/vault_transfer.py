from __future__ import annotations

import re
import shutil
from pathlib import Path

def scan_matching_files(source_root: Path, keywords: list[str]) -> list[Path]:
    """Return markdown files matching keywords (whole words) in filename or file content."""
    source_root = source_root.expanduser()
    
    # 1. Escape keywords and build the word-boundary pattern
    escaped = [re.escape(keyword) for keyword in keywords]
    pattern_string = rf"\b({'|'.join(escaped)})\b"
    pattern = re.compile(pattern_string, re.IGNORECASE)

    matches: list[Path] = []
    for md_file in source_root.rglob("*.md"):
        # Check filename first
        if pattern.search(md_file.name):
            matches.append(md_file)
            continue

        # Check content
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            # Skip unreadable files gracefully.
            continue

        if pattern.search(text):
            matches.append(md_file)

    return sorted(matches)

def copy_file(source_file: Path, source_root: Path, target_root: Path) -> Path:
    """Copy a file into the target vault preserving relative path structure."""
    source_root = source_root.expanduser()
    target_root = target_root.expanduser()

    relative = source_file.relative_to(source_root)
    destination = target_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, destination)
    return destination