from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process.vault_transfer import copy_file, scan_matching_files

SOURCE_ROOT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Learning Root"
).expanduser()
TARGET_ROOT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator"
).expanduser()

# Parse keywords from terminal arguments
if len(sys.argv) > 1:
    KEYWORDS = sys.argv[1:]
else:
    print("Error: You must provide at least one keyword.")
    print("Usage: python3 cli/transfer_learning_to_neural.py <keyword1> <keyword2>")
    sys.exit(1)


def main() -> None:
    print(f"Scanning for keywords: {', '.join(KEYWORDS)}...")
    files = scan_matching_files(SOURCE_ROOT, KEYWORDS)
    total = len(files)
    
    if total == 0:
        print("No matching files found. Exiting.")
        return
        
    print(f"Found {total} matching file(s). Starting automatic transfer...\n")

    copied = 0

    for index, source_file in enumerate(files, start=1):
        relative = source_file.relative_to(SOURCE_ROOT)
        
        # Copy the file immediately without asking
        destination = copy_file(source_file, SOURCE_ROOT, TARGET_ROOT)
        copied += 1
        print(f"[{index}/{total}] Copied: {relative}")

    print(f"\nSummary: {copied} file(s) successfully copied to Neural-orchestrator.")


if __name__ == "__main__":
    main()