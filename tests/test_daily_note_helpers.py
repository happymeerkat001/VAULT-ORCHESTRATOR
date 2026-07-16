import errno
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ingest import daily_note_helpers as helpers


def test_ensure_daily_note_creates_canonical_preamble(tmp_path):
    daily = tmp_path / "Daily Notes"

    note = helpers.ensure_daily_note_preamble(daily, "2026-07-04")

    content = note.read_text(encoding="utf-8")
    assert content.startswith("---\ntags:\n  - 📓\n---\n")
    assert (
        "Days:[[Daily Notes/2026-07-03 | Yesterday]] <== [[Daily Notes/2026-07-04]] ==> "
        "[[Daily Notes/2026-07-05|Tomorrow]]"
    ) in content


def test_ensure_daily_note_upgrades_frontmatter_only_without_duplicate_yaml(tmp_path):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text("---\ntags:\n  - 📓\n---\n\n## Vault Housekeep 🧹\n", encoding="utf-8")

    helpers.ensure_daily_note_preamble(daily, "2026-07-04")

    content = note.read_text(encoding="utf-8")
    assert content.count("---") == 2
    assert content.index("Days:[[Daily Notes/2026-07-03 | Yesterday]]") < content.index("## Vault Housekeep 🧹")


def test_ensure_daily_note_leaves_canonical_note_unchanged(tmp_path):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    original = helpers.build_note_preamble("2026-07-04") + "\nExisting body\n"
    note.write_text(original, encoding="utf-8")

    helpers.ensure_daily_note_preamble(daily, "2026-07-04")

    assert note.read_text(encoding="utf-8") == original


def test_read_text_with_retry_survives_icloud_deadlock(monkeypatch):
    attempts = {"count": 0}

    def fake_read_text(self, encoding="utf-8"):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return "ok"

    sleeps: list[float] = []
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(helpers.time, "sleep", sleeps.append)

    assert helpers.read_text_with_retry(Path("/tmp/x.md"), initial_delay=0, max_delay=0) == "ok"
    assert attempts["count"] == 3
    assert len(sleeps) == 2
