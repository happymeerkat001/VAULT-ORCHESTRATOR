"""Hermes-to-do parsing and state transitions for daily notes."""

import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

HERMES_HEADER = "## Hermes-to-do 🪶"
DAILY_NOTES_PATH = Path(".")
DAILY_NOTE_READ_ATTEMPTS = 10
DAILY_NOTE_READ_BASE_DELAY = 1.0
DAILY_NOTE_READ_MAX_DELAY = 32.0
DAILY_NOTE_WRITE_ATTEMPTS = 10
_read_daily_note_with_retry: Callable[[Path], str]
_write_daily_note_with_retry: Callable[[Path, str], None]

def configure(*, daily_notes_path: Path, header: str, read_daily_note_with_retry: Callable[[Path], str], write_daily_note_with_retry: Callable[[Path, str], None], read_attempts: int, read_base_delay: float, read_max_delay: float, write_attempts: int) -> None:
    globals().update(
        DAILY_NOTES_PATH=daily_notes_path,
        HERMES_HEADER=header,
        _read_daily_note_with_retry=read_daily_note_with_retry,
        _write_daily_note_with_retry=write_daily_note_with_retry,
        DAILY_NOTE_READ_ATTEMPTS=read_attempts,
        DAILY_NOTE_READ_BASE_DELAY=read_base_delay,
        DAILY_NOTE_READ_MAX_DELAY=read_max_delay,
        DAILY_NOTE_WRITE_ATTEMPTS=write_attempts,
    )


def find_today_note(today: str | None) -> Path:
    date_str = today or datetime.now().strftime("%Y-%m-%d")
    return DAILY_NOTES_PATH / (date_str + ".md")


def extract_hermes_section(note_text: str) -> tuple[int, int, list[tuple[int, str, str]]]:
    """Return (start_line, end_line, items) for the Hermes section.

    items: list of (line_index, status, text_without_checkbox) where status is
    one of 'open', 'in_progress', 'done'.
    """
    lines = note_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == HERMES_HEADER:
            start = i
            break
    if start is None:
        return -1, -1, []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            end = j
            break
    items: list[tuple[int, str, str]] = []
    for k in range(start + 1, end):
        raw = lines[k]
        m_done = re.match(r"^- \[x\]\s+(.+)$", raw.strip(), re.IGNORECASE)
        m_prog = re.match(r"^- \[~\]\s+(.+)$", raw.strip())
        m_fail = re.match(r"^- \[!\]\s+(.+)$", raw.strip())
        m_open = re.match(r"^- \[ \]\s+(.+)$", raw.strip())
        if m_done:
            items.append((k, "done", m_done.group(1)))
        elif m_prog:
            items.append((k, "in_progress", m_prog.group(1)))
        elif m_fail:
            items.append((k, "failed", m_fail.group(1)))
        elif m_open:
            items.append((k, "open", m_open.group(1)))
    return start, end, items


def next_open_item(note_text: str) -> tuple[int, str] | None:
    """Return (line_index, raw_task_text) of the next item to work on.

    Prefers in-progress (`- [~]`) items first (crash recovery), then open
    (`- [ ]`) items. Skips `done` (`- [x]`) and `failed` (`- [!]`) items.
    Returns None if the section is empty or all done.

    Failed items are NEVER picked up automatically. They are visible in the
    daily note for human review; rerun them by editing `- [!]` back to `- [ ]`.
    """
    _, _, items = extract_hermes_section(note_text)
    for line_idx, status, text in items:
        if status == "in_progress":
            return line_idx, _strip_running_suffix(text)
    for line_idx, status, text in items:
        if status == "open":
            return line_idx, text
    return None


def _strip_running_suffix(text: str) -> str:
    """Remove one or more '_(running)_' suffixes left by mark_in_progress on prior runs."""
    return re.sub(r"(\s*_\(running\)_)+\s*$", "", text).strip()


# Recognised effort hints at the start of a task line:
#   [1800s]    -> 1800 second wall clock budget for this task
#   [60i]      -> 60 LLM iterations for this task
#   [1800s,60i] -> both at once
# Examples:
#   - [ ] [1800s] research M3, M2.7, Claude Fable 5 and add to Ai Comparison Table
#   - [ ] [600s,40i] compare OpenRouter pricing
_EFFORT_HINT_RE = re.compile(
    r"^\s*\[(?P<flags>[^\]]+)\]\s*"
)


def parse_effort_hint(task_text: str) -> tuple[str, int, int]:
    """Extract optional [NNNs] and/or [NNNi] flags from the start of a task.

    Returns (cleaned_text, max_seconds, max_iterations). A 0 in either
    override field means "use the module default" (i.e. the worker should
    fall back to MAX_WALL_SECONDS / MAX_LOOP_ITERATIONS for that task).
    Unknown tokens inside the brackets are ignored and the bracket is left
    in place for the LLM to read.
    """
    max_seconds = 0
    max_iters = 0
    cleaned = task_text
    m = _EFFORT_HINT_RE.match(cleaned)
    if not m:
        return cleaned, max_seconds, max_iters
    raw_flags = m.group("flags")
    seconds_match = re.search(r"(\d+)\s*s\b", raw_flags)
    if seconds_match:
        max_seconds = int(seconds_match.group(1))
    iters_match = re.search(r"(\d+)\s*i\b", raw_flags)
    if iters_match:
        max_iters = int(iters_match.group(1))
    if max_seconds > 0 or max_iters > 0:
        cleaned = cleaned[m.end():].strip()
    else:
        # No recognised token — leave the text alone, including the bracket.
        return task_text, 0, 0
    return cleaned, max_seconds, max_iters


_ABS_MD_PATH_RE = re.compile(r"(/[^\n\r]+?\.md)")


def extract_absolute_md_paths(task_text: str) -> list[str]:
    """Return absolute .md paths mentioned in the task, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _ABS_MD_PATH_RE.findall(task_text):
        candidate = raw.rstrip(")],.;:'\"“”")
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def infer_target_note_path(task_text: str) -> str:
    """Best-effort guess at an explicit target markdown note path in the task.

    Preference signals:
    - nearby verbs like update/edit/write/append/add/merge
    - nearby phrases like target note/note/chart/table
    Negative signals:
    - nearby source-ish words like using/from/source/transcript
    - paths under Hermes Output/ (deliverables are usually not the target note)
    """
    paths = extract_absolute_md_paths(task_text)
    best_path = ""
    best_score = 0
    lower = task_text.lower()
    for path in paths:
        idx = lower.find(path.lower())
        if idx < 0:
            continue
        window = lower[max(0, idx - 80): min(len(lower), idx + len(path) + 80)]
        score = 0
        if any(token in window for token in ("target note", "target file", "target md")):
            score += 5
        if any(token in window for token in ("update", "edit", "write to", "append to", "add to", "merge into", "patch")):
            score += 4
        if any(token in window for token in (" note", " chart", " table", " comparison table", " file")):
            score += 2
        if any(token in window for token in ("using", "from", "source", "transcript", "based on", "read this")):
            score -= 4
        if "transcript" in path.lower():
            score -= 5
        if "/hermes output/" in path.lower():
            score -= 3
        if score > best_score:
            best_score = score
            best_path = path
    return best_path if best_score > 0 else ""


# ----------------------- daily-note mutation --------------------------------

def _normalize_task_signature(text: str) -> str:
    """Reduce a checkbox line (or raw task text) to a comparable signature.

    Strips the leading `- [x|~|!| ]` marker, any trailing worker suffixes
    (`_(running)_`, `_(→ see ...)_`, `_(failed: ...)_`), and collapses
    whitespace, lowercased. Used to re-locate a task by content.
    """
    t = re.sub(r"^- \[[ x~!]\]\s*", "", text.strip(), flags=re.IGNORECASE)
    t = re.sub(r"(\s*_\([^_]*\)_)+\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _locate_task_line(lines: list[str], line_idx: int, task_text: str) -> int:
    """Re-locate the task's checkbox line by CONTENT, tolerating line shifts.

    The daily note is concurrently mutated (ingest scripts, iPhone edits,
    iCloud merges) between the time a task is picked and the time its state
    is written back — especially under long [NNNs] budgets. Writing by the
    stale captured index stomps unrelated lines and leaves stale `- [~]`
    markers + duplicates. So: trust the index only if it still holds the
    same task; otherwise scan the Hermes section (then the whole file) for
    a checkbox line matching the task's text signature.

    Returns -1 if the task line no longer exists anywhere (e.g. the user
    deleted it mid-run); callers must skip the write in that case.
    """
    sig = _normalize_task_signature(task_text)
    if not sig:
        return -1
    if 0 <= line_idx < len(lines):
        cand = lines[line_idx].strip()
        if cand.startswith("- [") and _normalize_task_signature(cand).startswith(sig):
            return line_idx
    # Scan the Hermes section first.
    start = -1
    for i, l in enumerate(lines):
        if l.strip() == HERMES_HEADER:
            start = i
            break
    if start >= 0:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j].strip()
            if s.startswith("# ") or s.startswith("## "):
                end = j
                break
        for k in range(start + 1, end):
            s = lines[k].strip()
            if s.startswith("- [") and _normalize_task_signature(s).startswith(sig):
                return k
    # Last resort: whole-file scan (handles a header rename mid-run).
    for k, l in enumerate(lines):
        s = l.strip()
        if s.startswith("- [") and _normalize_task_signature(s).startswith(sig):
            return k
    return -1


def mark_in_progress(note_path: Path, line_idx: int, task_text: str) -> None:
    """Rewrite a `- [ ]` line as `- [~] ... _(running)_` and write back.

    The daily note is the single most-contended iCloud file in the vault, so
    both the read-modify-write round-trip and the write itself need a long,
    jittered retry budget. EDEADLK here is recoverable; the previous 5x/0.4s
    budget wedged the worker for minutes under load.
    """
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            idx = _locate_task_line(lines, line_idx, task_text)
            if idx < 0:
                print("[worker] mark_in_progress: task line not found; skipping write")
                return
            lines[idx] = "- [~] " + task_text + "  _(running)_"
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def _format_done_suffix(relpath: str) -> str:
    """Render the "see deliverable" marker with an Obsidian wikilink.

    A bare relative path is converted into a wikilink using the filename stem
    so it renders as an active link in Obsidian. Stems are unique enough within
    a single day's Hermes Output/ set to disambiguate without the date prefix
    leaking into the link text. If the relpath is empty or the link cannot be
    derived, fall back to a plain string in parentheses.
    """
    if not relpath:
        return "  _(no output file written)_"
    if not relpath.lower().endswith(".md"):
        return "  _(→ see " + relpath + ")_"
    name = relpath.rsplit("/", 1)[-1]
    stem = name[:-3] if name.lower().endswith(".md") else name
    return "  _(→ see [[%s]])_" % stem


def mark_done(
    note_path: Path,
    line_idx: int,
    original_text: str,
    output_relpath: str,
) -> None:
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            idx = _locate_task_line(lines, line_idx, original_text)
            if idx < 0:
                print("[worker] mark_done: task line not found; skipping write")
                return
            lines[idx] = (
                "- [x] " + original_text + _format_done_suffix(output_relpath)
            )
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def mark_open(note_path: Path, line_idx: int, original_text: str) -> None:
    """Restore a task to unchecked/open state after a failed run.

    Annotation contract: per the user's stated preference, the line itself
    stays as `- [ ] original_text` (unchecked, retryable). Any previously
    written annotation block directly below the parent is removed first so
    re-running the worker produces a fresh annotation rather than stacking
    stale `fail:` lines. If `annotation_lines` is provided, those new lines
    are appended (each indented with two spaces, matching the existing
    ingest-worker `fail:` shape).
    """
    lines: list[str] | None = None
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            break
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    if lines is None:
        assert last is not None
        raise last

    line_idx = _locate_task_line(lines, line_idx, original_text)
    if line_idx < 0:
        print("[worker] mark_open: task line not found; skipping write")
        return
    # Strip any `_(running)_` suffix that mark_in_progress added so the
    # restored line is a clean `- [ ] original_text` and not a stale
    # in-progress marker pretending to be unchecked.
    lines[line_idx] = "- [ ] " + _strip_running_suffix(original_text)

    # Strip any prior worker annotation block. Worker-emitted annotation
    # lines are tagged with `<!-- h:... -->` HTML comments so the stripper
    # can identify them unambiguously without false-positive matching
    # against user-written indented content. Obsidian renders HTML comments
    # as nothing, so they are visually invisible but durable for the parser.
    # (annotate_failure also strips before inserting, but doing it here too
    # keeps the read-modify-write atomic in case the subsequent annotate
    # write fails on iCloud EDEADLK.)
    end = line_idx + 1
    while end < len(lines) and lines[end].startswith("  ") and "<!-- h:" in lines[end]:
        end += 1
    if end > line_idx + 1:
        del lines[line_idx + 1:end]

    last = None
    for attempt in range(DAILY_NOTE_WRITE_ATTEMPTS):
        try:
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_WRITE_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def get_retry_count(note_path: Path, line_idx: int, task_text: str) -> int:
    """Read the retry counter from the worker annotation block under a task.

    The counter is written by annotate_failure as
    `  <!-- h:retry --> retry: N/M`. Returns 0 when no counter exists
    (first failure) or the note/line cannot be read.
    """
    try:
        lines = _read_daily_note_with_retry(note_path).splitlines()
    except OSError:
        return 0
    idx = _locate_task_line(lines, line_idx, task_text)
    if idx < 0:
        return 0
    j = idx + 1
    while j < len(lines) and lines[j].startswith("  ") and "<!-- h:" in lines[j]:
        m = re.search(r"<!-- h:retry -->\s*retry:\s*(\d+)", lines[j])
        if m:
            return int(m.group(1))
        j += 1
    return 0


def mark_failed(note_path: Path, line_idx: int, original_text: str, reason: str) -> None:
    """Mark a task sticky-failed: `- [!] text  _(failed: reason)_`.

    Per the documented contract, `- [!]` items are NEVER picked up
    automatically; the user retries by editing the marker back to `- [ ]`.
    The worker annotation block below the line is removed — the failure
    reason now lives inline on the line itself.
    """
    short = re.sub(r"\s+", " ", reason).strip()[:160]
    last: Exception | None = None
    for attempt in range(DAILY_NOTE_WRITE_ATTEMPTS):
        try:
            lines = _read_daily_note_with_retry(note_path).splitlines()
            idx = _locate_task_line(lines, line_idx, original_text)
            if idx < 0:
                print("[worker] mark_failed: task line not found; skipping write")
                return
            lines[idx] = (
                "- [!] " + _strip_running_suffix(original_text)
                + "  _(failed: %s)_" % short
            )
            end = idx + 1
            while end < len(lines) and lines[end].startswith("  ") and "<!-- h:" in lines[end]:
                end += 1
            if end > idx + 1:
                del lines[idx + 1:end]
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_WRITE_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


def annotate_failure(note_path: Path, line_idx: int, task_text: str, reason: str, suggestion: str, extra: list[str] | None = None) -> None:
    """Insert a `fail:` annotation block directly below the parent task line.

    Idempotent: any prior worker annotation block directly below `line_idx`
    is removed first, then the new block is inserted in its place. The
    block shape is:

        - [ ] original task text
          <!-- h:fail --> fail: <reason>
          <!-- h:try -->  try: <suggestion>
          [<!-- h:split --> split into:]
          [<!-- h:child --> - [ ] <child>]

    Each line is tagged with an HTML comment marker so the stripper can
    identify and replace it without false-positive matches on user-written
    indented content. Obsidian renders the comment marker as nothing, so
    the visible output is just the indented annotation.

    `extra` is used by `process_one` to append a "split into:" header plus
    2-4 child task sub-bullets when the failure was a "task too long"
    timeout/iter-cap. The full read-modify-write happens inside one retry
    loop so a partial annotation cannot survive an iCloud EDEADLK mid-write.
    """
    extras = extra or []
    # Build the new annotation lines (each starts with two spaces).
    new_lines: list[str] = ["  <!-- h:fail --> fail: %s" % reason, "  <!-- h:try --> try: %s" % suggestion]
    for line in extras:
        if line.startswith("- [ ] "):
            new_lines.append("  <!-- h:child --> " + line)
        elif line == "split into:":
            new_lines.append("  <!-- h:split --> " + line)
        elif line.startswith("retry:"):
            new_lines.append("  <!-- h:retry --> " + line)
        else:
            new_lines.append("  <!-- h:info --> " + line)

    last: Exception | None = None
    for attempt in range(DAILY_NOTE_WRITE_ATTEMPTS):
        try:
            text = _read_daily_note_with_retry(note_path)
            lines = text.splitlines()
            line_idx = _locate_task_line(lines, line_idx, task_text)
            if line_idx < 0:
                print("[worker] annotate_failure: task line not found; skipping write")
                return
            # Strip any existing worker annotation block directly below the parent.
            end = line_idx + 1
            while end < len(lines) and lines[end].startswith("  ") and "<!-- h:" in lines[end]:
                end += 1
            if end > line_idx + 1:
                del lines[line_idx + 1:end]
            # Insert the new annotation block in the cleared slot.
            for offset, new_line in enumerate(new_lines, start=1):
                lines.insert(line_idx + offset, new_line)
            _write_daily_note_with_retry(note_path, "\n".join(lines) + "\n")
            return
        except OSError as exc:
            last = exc
            if attempt == DAILY_NOTE_WRITE_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** attempt), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last
