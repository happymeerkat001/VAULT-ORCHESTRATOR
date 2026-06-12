import errno
import importlib.util
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
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
