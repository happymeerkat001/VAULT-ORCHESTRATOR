import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import backfill_youtube_ai_summary as backfill


def test_note_needs_ai_summary_detects_missing_section():
    content = """# Video Title

**Source:** https://youtu.be/abc123
**Transcript source:** transcript.lol

---

## Transcript

hello world
"""
    assert backfill.note_needs_ai_summary(content) is True


def test_inject_ai_summary_adds_block_before_divider():
    content = """# Video Title

**Source:** https://youtu.be/abc123
**Transcript source:** transcript.lol

---

## Transcript

hello world
"""
    upgraded = backfill.inject_ai_summary(content, "Useful summary")

    assert "## AI Summary\n\nUseful summary" in upgraded
    assert upgraded.index("## AI Summary") < upgraded.index("---")
    assert upgraded.endswith("hello world\n")


def test_inject_ai_summary_replaces_existing_block():
    content = """# Video Title

**Source:** https://youtu.be/abc123

## AI Summary

Old summary

---

## Transcript

hello world
"""
    upgraded = backfill.inject_ai_summary(content, "New summary")

    assert "Old summary" not in upgraded
    assert "## AI Summary\n\nNew summary" in upgraded
