from hermes import taskstate
import importlib.util
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
WORKER_PATH = REPO / "cli" / "hermes_worker.py"


def load_worker_module():
    spec = importlib.util.spec_from_file_location("hermes_worker_for_test", WORKER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_next_open_item_recovers_running_item_before_open_item():
    note = """# Day

## Hermes-to-do 🪶
- [ ] later task
- [~] interrupted task  _(running)_  _(running)_
- [!] sticky failed task
## Other
- [ ] not a Hermes task
"""

    assert taskstate.next_open_item(note) == (4, "interrupted task")


def test_next_open_item_skips_done_and_sticky_failed_items():
    note = """## Hermes-to-do 🪶
- [x] completed  _(→ see [[output]])_
- [!] failed  _(failed: permanent issue)_
"""

    assert taskstate.next_open_item(note) is None


def test_normalize_task_signature_strips_worker_suffixes_and_collapses_space():
    assert taskstate._normalize_task_signature(
        "- [~]  Research   model pricing  _(running)_  _(failed: ignored)_"
    ) == "research model pricing"


def test_locate_task_line_recovers_after_concurrent_line_shift():
    lines = [
        "## Hermes-to-do 🪶",
        "- [ ] unrelated task",
        "- [~] target task  _(running)_",
        "## Later",
    ]

    assert taskstate._locate_task_line(lines, 1, "target task") == 2


def test_parse_effort_hint_accepts_combined_flags_and_preserves_unknown_brackets():
    assert taskstate.parse_effort_hint("[600s, 40i] research the task") == (
        "research the task", 600, 40
    )
    assert taskstate.parse_effort_hint("[urgent] research the task") == (
        "[urgent] research the task", 0, 0
    )


def test_worker_uses_the_extracted_taskstate_module():
    worker = load_worker_module()

    assert worker.next_open_item is taskstate.next_open_item
    assert worker.mark_failed is taskstate.mark_failed
