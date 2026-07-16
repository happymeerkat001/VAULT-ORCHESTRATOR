import errno
import importlib.util
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
MODULE_PATH = REPO / "ingest" / "briefing_sync.py"
spec = importlib.util.spec_from_file_location("briefing_sync", MODULE_PATH)
assert spec is not None
briefing_sync = importlib.util.module_from_spec(spec)
sys.modules["briefing_sync"] = briefing_sync
assert spec.loader is not None
spec.loader.exec_module(briefing_sync)


def test_read_text_with_retry_survives_long_icloud_deadlock(monkeypatch):
    """iCloud can hold yesterday's daily note longer than the old 10 tries."""
    attempts = {"count": 0}

    def fake_read_text(self, encoding="utf-8"):
        attempts["count"] += 1
        if attempts["count"] <= 12:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return "ok"

    sleeps: list[float] = []
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(briefing_sync.time, "sleep", sleeps.append)

    result = briefing_sync.read_text_with_retry(Path("/tmp/yesterday.md"), initial_delay=0, max_delay=0)

    assert result == "ok"
    assert attempts["count"] == 13
    assert len(sleeps) == 12


def test_get_calendar_bounds_can_use_requested_backfill_date():
    start, end, lookahead = briefing_sync.get_calendar_bounds("2026-06-29")

    assert lookahead == 2
    assert start == "2026-06-29T05:00:00Z"
    assert end == "2026-07-01T04:59:59Z"


def test_write_degraded_briefing_creates_morning_briefing(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    monkeypatch.setattr(briefing_sync, "DAILY_NOTES_PATH", daily)

    out = briefing_sync.write_degraded_briefing(
        "2026-07-03",
        "Google OAuth token expired",
        "python3 cli/google_reauth.py",
    )

    content = out.read_text(encoding="utf-8")
    assert "## Morning Briefing ☀️" in content
    assert "#degraded-sync" in content
    assert "Briefing Sync Failed: Google OAuth token expired" in content
    assert "python3 cli/google_reauth.py" in content
    assert "## Calendar 📅" in content
    assert "## Email Highlights 📧" in content


def test_write_degraded_briefing_preserves_local_rollover(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    monkeypatch.setattr(briefing_sync, "DAILY_NOTES_PATH", daily)

    def fake_rollover(date_str):
        assert date_str == "2026-07-03"
        return (
            ["- [ ] Think about vault hygiene"],
            ["- [ ] Call Sam", "- [ ] Re-run briefing"],
            ["- [ ] Hermes check"],
        )

    monkeypatch.setattr(briefing_sync, "get_yesterday_unchecked", fake_rollover)

    out = briefing_sync.write_degraded_briefing(
        "2026-07-03",
        "Calendar fetch failed",
        "python3 ingest/briefing_sync.py --date 2026-07-03",
    )

    content = out.read_text(encoding="utf-8")
    assert "## Morning Briefing ☀️" in content
    assert "Think about vault hygiene" in content
    assert "Call Sam" in content
    assert "Re-run briefing" in content
    assert "Hermes check" in content
    # Hardcoded "Repair the daily briefing sync" placeholder must not appear
    # when local rollover succeeds.
    assert "Repair the daily briefing sync" not in content


def test_write_briefing_upgrades_frontmatter_only_note_without_duplicate_yaml(tmp_path, monkeypatch):
    daily = tmp_path / "Daily Notes"
    daily.mkdir(parents=True)
    note = daily / "2026-07-04.md"
    note.write_text("---\ntags:\n  - 📓\n---\n\n## Vault Housekeep 🧹\n", encoding="utf-8")
    monkeypatch.setattr(briefing_sync, "DAILY_NOTES_PATH", daily)

    briefing_sync.write_briefing("2026-07-04", "## Morning Briefing ☀️\n\nBriefed.\n")

    content = note.read_text(encoding="utf-8")
    assert content.count("---") == 2
    assert content.index("Days:[[Daily Notes/2026-07-03 | Yesterday]]") < content.index("## Vault Housekeep 🧹")
    assert "## Vault Housekeep 🧹" in content
    assert "## Morning Briefing ☀️" in content


def test__get_json_retries_transient_connection_errors(monkeypatch):
    attempts = {"count": 0}

    def fake_urlopen(req, timeout):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionResetError("transient")
        class FakeResp:
            def read(self):
                return b'{"ok": 1}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return FakeResp()

    monkeypatch.setattr(briefing_sync.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(briefing_sync.time, "sleep", lambda _: None)

    result = briefing_sync._get_json("https://example.invalid/x", "token")

    assert result == {"ok": 1}
    assert attempts["count"] == 3

