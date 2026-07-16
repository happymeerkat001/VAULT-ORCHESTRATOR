import importlib.util
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "cli" / "daily_briefing_catchup.py"
spec = importlib.util.spec_from_file_location("daily_briefing_catchup", MODULE_PATH)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["daily_briefing_catchup"] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def test_catchup_creates_note_and_runs_briefing_when_missing(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)
    calls: list[str] = []

    def fake_run(date_str: str) -> int:
        calls.append(date_str)
        (daily / f"{date_str}.md").write_text(
            "---\ntags:\n  - 📓\n---\n"
            "Days:[[Daily Notes/2026-07-03 | Yesterday]] <== [[Daily Notes/2026-07-04]] ==> [[Daily Notes/2026-07-05|Tomorrow]]\n\n"
            "## Morning Briefing ☀️\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(mod, "run_briefing_sync", fake_run)

    assert mod.catch_up("2026-07-04") == 0
    assert calls == ["2026-07-04"]


def test_catchup_preserves_housekeep_content_before_briefing(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text("---\ntags:\n  - 📓\n---\n\n## Vault Housekeep 🧹\nProposal ready.\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)

    def fake_run(date_str: str) -> int:
        existing = note.read_text(encoding="utf-8")
        note.write_text(existing + "\n## Morning Briefing ☀️\nBriefed.\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(mod, "run_briefing_sync", fake_run)

    assert mod.catch_up("2026-07-04") == 0
    content = note.read_text(encoding="utf-8")
    assert "## Vault Housekeep 🧹" in content
    assert "Proposal ready." in content
    assert "## Morning Briefing ☀️" in content


def test_catchup_skips_when_briefing_present_without_force(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text(
        "---\ntags:\n  - 📓\n---\n"
        "Days:[[Daily Notes/2026-07-03 | Yesterday]] <== [[Daily Notes/2026-07-04]] ==> [[Daily Notes/2026-07-05|Tomorrow]]\n\n"
        "## Morning Briefing ☀️\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)
    monkeypatch.setattr(mod, "run_briefing_sync", lambda _date: (_ for _ in ()).throw(AssertionError("should not run")))

    assert mod.catch_up("2026-07-04") == 0


def test_catchup_force_runs_even_when_briefing_present(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text(
        "---\ntags:\n  - 📓\n---\n"
        "Days:[[Daily Notes/2026-07-03 | Yesterday]] <== [[Daily Notes/2026-07-04]] ==> [[Daily Notes/2026-07-05|Tomorrow]]\n\n"
        "## Morning Briefing ☀️\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)
    calls: list[str] = []
    monkeypatch.setattr(mod, "run_briefing_sync", lambda date_str: calls.append(date_str) or 0)

    assert mod.catch_up("2026-07-04", force=True) == 0
    assert calls == ["2026-07-04"]


def test_catchup_reports_degraded_nonzero_status(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    monkeypatch.setattr(mod, "DAILY_NOTES_PATH", daily)

    def fake_run(date_str: str) -> int:
        (daily / f"{date_str}.md").write_text(
            "---\ntags:\n  - 📓\n---\n"
            "Days:[[Daily Notes/2026-07-03 | Yesterday]] <== [[Daily Notes/2026-07-04]] ==> [[Daily Notes/2026-07-05|Tomorrow]]\n\n"
            "## Morning Briefing ☀️\n\n#degraded-sync\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(mod, "run_briefing_sync", fake_run)

    assert mod.catch_up("2026-07-04") == 1
