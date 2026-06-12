#!/usr/bin/env python3
"""
preflight.py - Syntax-check every Python file the scheduled jobs import.

Catches SyntaxError regressions (e.g. Python 3.12-only f-strings under 3.11)
BEFORE a launchd job tries to import the module and silently dies in its err
log. On any compile failure, prints a loud one-line summary to stdout (which
launchd will capture in the .out log) and writes a multi-line report to the
log file. Exits non-zero so launchd treats it as a hard failure.

Manual run:
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/preflight.py
  python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/preflight.py --root /path/to/repo

Scheduled via:
  ~/Library/LaunchAgents/com.leon.preflight.python.plist
  Logs: ~/.claude/logs/preflight.out.log and ~/.claude/logs/preflight.err.log
"""

from __future__ import annotations

import argparse
import compileall
import datetime
import os
import py_compile
import sys
from pathlib import Path


DEFAULT_ROOT = Path("/Users/leon/Documents/Code/Obsidian-vault-orchestrator")
LOG_DIR = Path.home() / ".claude" / "logs"
LOG_FILE = LOG_DIR / "preflight.log"

# Directories whose *.py files must compile cleanly.
# Keep in sync with the modules imported by the launchd-driven scripts:
#   cli/*      - all manual + scheduled ingest/CLI scripts
#   ingest/*   - the API fetchers
#   scripts/*  - process_ingest.py (OCR post-processor)
TARGET_DIRS: tuple[str, ...] = ("cli", "ingest", "scripts")


def parse_args() -> argparse.Namespace:
    desc = (globals().get("__doc__") or "preflight.py").splitlines()[0]
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Repo root to scan (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file OK lines; only print summary on failure.",
    )
    return parser.parse_args()


def collect_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for sub in TARGET_DIRS:
        sub_path = root / sub
        if not sub_path.is_dir():
            continue
        for path in sorted(sub_path.rglob("*.py")):
            if path.name == "__pycache__":
                continue
            files.append(path)
    return files


def compile_one(path: Path) -> tuple[bool, str]:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        return False, str(exc).strip() or "compile error"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def write_log(failures: list[tuple[Path, str]], checked: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    lines = [f"[{timestamp}] preflight: scanned {checked} file(s)"]
    if not failures:
        lines.append("preflight: OK - all Python files compile")
    else:
        lines.append(f"preflight: FAIL - {len(failures)} file(s) did not compile")
        for path, reason in failures:
            lines.append(f"  - {path}: {reason}")
    LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = args.root.expanduser()
    if not root.is_dir():
        print(f"[preflight] root not found: {root}", file=sys.stderr)
        return 2

    files = collect_python_files(root)
    if not files:
        print(f"[preflight] no .py files found under {root}/{{{','.join(TARGET_DIRS)}}}")
        return 2

    failures: list[tuple[Path, str]] = []
    for path in files:
        ok, reason = compile_one(path)
        if ok:
            if not args.quiet:
                print(f"[preflight] OK   {path.relative_to(root)}")
        else:
            print(f"[preflight] FAIL {path.relative_to(root)}: {reason}")
            failures.append((path, reason))

    # Also run compileall.quiet as a fast belt-and-suspenders sweep. It
    # returns False if any file in the directory failed to compile.
    all_clean = compileall.compile_dir(
        str(root / "cli"),
        quiet=1,
        maxlevels=10,
    ) and compileall.compile_dir(
        str(root / "ingest"),
        quiet=1,
        maxlevels=10,
    )

    write_log(failures, checked=len(files))

    if failures or not all_clean:
        print(
            f"[preflight] FAIL - {len(failures)} file(s) did not compile. "
            f"See {LOG_FILE}.",
            file=sys.stderr,
        )
        return 1
    print(f"[preflight] OK - {len(files)} Python file(s) compile cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
