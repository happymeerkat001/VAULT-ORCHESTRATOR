import importlib.util
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
MODULE_PATH = REPO / "ingest" / "weekly_housekeep_propose.py"
spec = importlib.util.spec_from_file_location("weekly_housekeep_propose", MODULE_PATH)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["weekly_housekeep_propose"] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def _empty_note(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nttags: 📓\n---\n", encoding="utf-8")
    return path


def _real_note(path: Path, body: str = "Body that is meaningful and longer than ten chars.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntags: 📓\n---\n# Title\n\n" + body + "\n", encoding="utf-8")
    return path


def test_score_action_confidence_thresholds():
    assert mod.score_action_confidence({"kind": "EMPTY_NOTE", "evidence": {"size_bytes": 12}}) >= 0.95
    assert mod.score_action_confidence({"kind": "ORPHAN_TOPIC", "evidence": {"backlinks": 0}}) >= 0.85
    assert mod.score_action_confidence(
        {"kind": "NEAR_DUPLICATE", "evidence": {"pair": ("A.md", "B.md"), "score": 0.55}}
    ) < 0.85


def test_propose_actions_classifies_empty_notes_as_movedeferred(tmp_path):
    vault = tmp_path / "vault"
    _empty_note(vault / "Archive" / "stale-old-2024.md")
    _real_note(vault / "Claude Fable" / "active-idea.md")

    actions = mod.propose_actions(vault, today_iso="2026-07-04")

    empty = [a for a in actions if a["kind"] == "EMPTY_NOTE"]
    assert empty, "expected EMPTY_NOTE proposal"
    assert empty[0]["proposal"] in ("MERGE", "DEFER", "RENAME")
    assert "stale-old-2024.md" in empty[0]["target"]


def test_propose_actions_groups_archive_files_older_than_threshold(tmp_path):
    vault = tmp_path / "vault"
    archive = vault / "Archive" / "Raw Ingest"
    archive.mkdir(parents=True, exist_ok=True)
    old = archive / "old-2024.md"
    old.write_text("# Title\n\n" + ("x" * 600) + "\n", encoding="utf-8")
    # Force mtime to ~400 days before today so the staleness check triggers.
    import os
    import time as _t
    old_time = _t.time() - (400 * 86400)
    os.utime(old, (old_time, old_time))

    (vault / "Claude Fable").mkdir(parents=True, exist_ok=True)

    actions = mod.propose_actions(vault, today_iso="2026-07-04", stale_days=180)

    archive_actions = [
        a for a in actions if "Archive" in a["target"] and a["proposal"] in ("DEFER", "MERGE")
    ]
    assert archive_actions, "expected Archive cleanup proposal"
    paths = {a["target"] for a in archive_actions}
    assert any("old-2024.md" in p for p in paths)


def test_format_addendum_includes_housekeep_section_and_rollover_marker(tmp_path):
    actions = [
        {"kind": "EMPTY_NOTE", "proposal": "MERGE", "confidence": 0.97,
         "target": "Archive/empty.md", "reason": "12 bytes", "src": "test"}
    ]
    block = mod.format_addendum(date_str="2026-07-04", proposal_path=Path("/tmp/proposal.md"),
                                actions=actions, proposal_id="wk2026-27")

    assert "## Vault Housekeep 🧹" in block
    assert "<!-- housekeep-id:wk2026-27 -->" in block
    assert "proposal_id=wk2026-27" in block or "wk2026-27" in block
    assert "MERGE" in block
    assert "Archive/empty.md" in block
    # 7-day rollover marker present
    assert re.search(r"<!-- housekeep-until:20\d{2}-\d{2}-\d{2} -->", block)

    # Idempotency: block keys roll forward with new days
    fresh = mod.format_addendum(date_str="2026-07-05", proposal_path=Path("/tmp/proposal.md"),
                                actions=actions, proposal_id="wk2026-27")
    assert "<!-- housekeep-id:wk2026-27 -->" in fresh
    assert "Carry-over from " in fresh


def test_apply_proposals_requires_explicit_approve(monkeypatch, tmp_path):
    approve_file = tmp_path / "approve.json"
    approve_file.write_text('{"ok": true}', encoding="utf-8")

    called = {"n": 0}
    def fake_move(src, dst):
        called["n"] += 1

    monkeypatch.setattr(mod.shutil, "move", fake_move)

    # Without approve flag must refuse
    try:
        mod.apply_proposals(proposal_path=tmp_path / "missing.md", approve_path=None)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit when no --approve provided")

    # With approve file present, perform a no-op action and confirm shutil.move was called
    proposal = tmp_path / "weekly.md"
    src_note = tmp_path / "src.md"
    _real_note(src_note)
    proposal.write_text(
        '[{"kind": "EMPTY_NOTE", "proposal": "MERGE", "confidence": 0.97,'
        ' "target": "' + str(src_note) + '", "reason": "stub", "src": "test"}]',
        encoding="utf-8",
    )
    mod.apply_proposals(proposal_path=proposal, approve_path=approve_file)
    assert called["n"] >= 1

def test_write_addendum_creates_canonical_daily_note(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)

    note = mod.write_addendum_to_daily_note("2026-07-04", "## Vault Housekeep 🧹\n\nProposal ready.")

    content = note.read_text(encoding="utf-8")
    assert content.startswith("---\ntags:\n  - 📓\n---\n")
    assert "Days:[[Daily Notes/2026-07-03 | Yesterday]]" in content
    assert "## Vault Housekeep 🧹" in content
    assert "Proposal ready." in content


def test_write_addendum_upgrades_frontmatter_only_daily_note_without_duplicate_yaml(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text("---\ntags:\n  - 📓\n---\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)

    mod.write_addendum_to_daily_note("2026-07-04", "## Vault Housekeep 🧹\n\nProposal ready.")

    content = note.read_text(encoding="utf-8")
    assert content.count("---") == 2
    assert "Days:[[Daily Notes/2026-07-03 | Yesterday]]" in content
    assert content.index("Days:[[Daily Notes/2026-07-03 | Yesterday]]") < content.index("## Vault Housekeep 🧹")

