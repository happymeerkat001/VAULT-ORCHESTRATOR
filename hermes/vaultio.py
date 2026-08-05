"""iCloud-aware vault path and text I/O primitives."""

import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

VAULT_ROOT = Path(".")
DAILY_NOTE_READ_ATTEMPTS = 10
DAILY_NOTE_READ_BASE_DELAY = 1.0
DAILY_NOTE_READ_MAX_DELAY = 32.0
DAILY_NOTE_WRITE_ATTEMPTS = 10

def configure(*, vault_root: Path, read_attempts: int, read_base_delay: float, read_max_delay: float, write_attempts: int) -> None:
    globals().update(VAULT_ROOT=vault_root, DAILY_NOTE_READ_ATTEMPTS=read_attempts, DAILY_NOTE_READ_BASE_DELAY=read_base_delay, DAILY_NOTE_READ_MAX_DELAY=read_max_delay, DAILY_NOTE_WRITE_ATTEMPTS=write_attempts)

def safe_path(raw: str) -> Path:
    """Resolve `raw` to an absolute path and assert it lives under VAULT_ROOT.

    Rejects '..', absolute paths outside the vault, and symlinks pointing out.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (VAULT_ROOT / p).resolve()
    else:
        p = p.resolve()
    vault = VAULT_ROOT.resolve()
    try:
        p.relative_to(vault)
    except ValueError as exc:
        raise ValueError("path escapes vault root: %s" % raw) from exc
    return p


def safe_writable_path(raw: str) -> Path:
    """Resolve a writable path inside the vault.

    By user request, the worker may write anywhere inside VAULT_ROOT. The only
    sandbox boundary is the vault root itself; paths outside the vault are
    rejected by safe_path().
    """
    return safe_path(raw)


# ----------------------------- tools ----------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the UTF-8 text of a file inside the vault. Returns up to N lines (clamped to a hard ceiling of 300 lines per call to protect the context budget; if you need more, re-read with a tighter scope).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the vault, or absolute path inside the vault."},
                    "max_lines": {"type": "integer", "description": "Cap on lines returned (default 200, hard ceiling 300)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List immediate entries of a directory inside the vault. Returns names + a flag marking directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path inside the vault."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Recursively find files under a directory whose name matches a glob pattern (case-insensitive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Directory to search."},
                    "glob": {"type": "string", "description": "Filename pattern, e.g. '*Hermes*' or '*.md'."},
                },
                "required": ["root", "glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a public URL and return its content as plain text. "
                "HTML is stripped to text; very long pages are truncated to "
                "WEB_FETCH_MAX_CHARS chars. Use this to read docs, blog posts, "
                "API references, or any other web resource needed to complete "
                "the task. Read-only; cannot be used to write anywhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web via DuckDuckGo HTML and return the top "
                "result titles, snippets, and URLs. No API key required. Use "
                "this when you need to discover URLs or compare options. "
                "Read-only; cannot be used to write anywhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'MiniMax M3 release benchmarks'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on number of results returned (default 8, max 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_directory",
            "description": "Create a directory (with parents) inside the vault. Idempotent.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file/directory inside the vault. Creates parent dirs of the destination if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dst": {"type": "string"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_output_file",
            "description": "Write text to a file anywhere inside the vault (overwrites if exists). Use this for final deliverables in Hermes Output/ and for direct note updates when the task asks for it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _read_text_with_retry(path: Path, attempts: int = 5) -> str:
    # Backwards-compat shim for any external caller. The default 5x/0.4s
    # budget is no longer the right answer for iCloud-resident files — see
    # _read_text_with_iCloud_retry below. New code should call the iCloud
    # variant directly; this shim stays because the third-party tools
    # (ingest scripts, ad-hoc callers) still rely on the 5-arg signature.
    last = None
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            time.sleep(0.4 * (2 ** i))
    raise last  # type: ignore[misc]


def _write_text_with_retry(path: Path, content: str, attempts: int = 5) -> None:
    last = None
    for i in range(attempts):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last = exc
            time.sleep(0.4 * (2 ** i))
    raise last  # type: ignore[misc]


# ---------------------- iCloud EDEADLK self-heal -----------------------------
#
# iCloud (the Apple "CloudDocs" daemon, com.apple.CloudDocs) holds a
# byte-range lock on every iCloud-resident file the first time a process
# opens it. If the local copy has been evicted to the cloud, macOS
# blocks the read with errno 11 (EDEADLK, "Resource deadlock avoided") or
# errno 35 (EAGAIN, "Resource temporarily unavailable") until the file
# has been re-materialized from iCloud. The default Python read path
# surfaces that as a plain OSError, and the only way to break the lock
# is to ask the daemon to re-download the file via `brctl download`.
#
# The previous retry loop (5 attempts, ~12s total backoff) gave up before
# iCloud released the lock, so every read_file call against an evicted
# transcript (HT102, and any other iCloud-cold file the task happens to
# point at) returned "Resource deadlock avoided" and the LLM aborted.
# The fix is two layers:
#
#   1. _ensure_icloud_downloaded(path) shells out to `brctl download`
#      (macOS built-in, no new deps) on the first EDEADLK/EAGAIN. The
#      daemon is async, so the helper then polls with the same jittered
#      backoff the daily-note reads use.
#
#   2. _read_text_with_iCloud_retry wraps the read with the daily-note
#      retry budget (10 attempts, 1s base, 32s cap) and triggers the
#      download helper on the first lock error. This fixes read_file for
#      every vault file, not just the daily note.
#
# `brctl` is only present on macOS, so the helpers fall back to a plain
# retry on other platforms (where errno 11/35 is never produced anyway).
_ICLOUD_LOCK_ERRNOS = (11, 35)  # EDEADLK, EAGAIN


def _is_icloud_lock_error(exc: BaseException) -> bool:
    """Return True if `exc` looks like an iCloud materialization lock.

    EDEADLK (11) is the canonical "Resource deadlock avoided" Python
    surfaces from macOS during iCloud cold reads. EAGAIN (35) is the
    byte-range-lock counterpart the daemon can also raise. Anything
    else (FileNotFoundError, IsADirectoryError, PermissionError) is a
    real read error and should not trigger a `brctl download`.
    """
    errno = getattr(exc, "errno", None)
    if errno in _ICLOUD_LOCK_ERRNOS:
        return True
    msg = str(exc).lower()
    return "resource deadlock" in msg or "resource temporarily unavailable" in msg


def _ensure_icloud_downloaded(path: Path, attempts: int = 6) -> bool:
    """Ask iCloud to materialize `path`, then poll until readable.

    Returns True if the file is readable (or was never iCloud-locked).
    Returns False if `brctl` is missing, the file is outside the iCloud
    container, or the daemon hasn't finished the download within the
    attempt budget. Non-macOS platforms are a no-op (return True).
    Stdlib subprocess only; no new dependencies.
    """
    if sys.platform != "darwin":
        return True
    if not path.exists():
        return False
    # Kick the daemon. brctl download is async — it returns ~immediately
    # and the file becomes readable once the download completes. We fire
    # one call and then poll the read; if the poll keeps EDEADLK'ing,
    # try one more download (in case the daemon dropped the request).
    brctl = shutil.which("brctl")
    if not brctl:
        return False
    target = str(path)
    try:
        subprocess.run(
            [brctl, "download", target],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        # brctl returning non-zero is not fatal — the daemon sometimes
        # errors on paths that are already local. Keep polling.
        pass
    for i in range(attempts):
        try:
            # A 0-byte open + immediate close is enough to detect "the
            # lock is gone" without materializing the full content. Use
            # the same backoff curve as the daily-note reads.
            with open(path, "rb") as fh:
                fh.read(1)
            return True
        except OSError as exc:
            if not _is_icloud_lock_error(exc) and i > 0:
                # Stop early on a real I/O error (permission, missing
                # file, etc.) — further brctl calls won't help.
                return False
            delay = min(1.0 * (2 ** i), 16.0)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
        if i in (1, 3):
            # Re-kick the daemon a couple of times in case the first
            # request was coalesced or dropped. Cheap; brctl is idempotent.
            try:
                subprocess.run(
                    [brctl, "download", target],
                    capture_output=True,
                    timeout=10,
                )
            except (subprocess.SubprocessError, OSError):
                pass
    return False


def _read_text_with_iCloud_retry(path: Path) -> str:
    """Read `path` with a long, jittered retry budget and iCloud self-heal.

    This replaces the old `_read_text_with_retry` for the worker tool set.
    On the first EDEADLK/EAGAIN, shell out to `brctl download` to ask
    iCloud to materialize the file, then continue polling. Same budget
    shape as `_read_daily_note_with_retry` (10 attempts, 1s base, 32s
    cap, ±20% jitter) so a single helper covers both the daily note and
    every other iCloud-resident file in the vault.
    """
    last: Exception | None = None
    triggered = False
    for i in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            if _is_icloud_lock_error(exc) and not triggered:
                # One-shot: shell out to brctl exactly once on the first
                # lock error. If that doesn't unstick the file, the
                # remaining retries just sleep and try again. We don't
                # spam brctl on every iteration — the daemon is async
                # and the call doesn't return until the file is local.
                triggered = True
                _ensure_icloud_downloaded(path)
                continue
            if i == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** i), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
    raise last


# Sentinel prefix returned by `tool_read_file` when the file is still
# iCloud-locked after exhausting the retry budget. The LLM sees a soft
# message instead of a raised exception, so it can keep working on
# other files in the run and circle back to the locked one later. The
# sentinel is parseable by the LLM but unlikely to appear naturally in
# a vault file, so there's no ambiguity in the tool output.
_ICLOUD_LOCK_SENTINEL = "(file locked by iCloud sync — download queued, retry this file later in the run)"


def _preflight_icloud_downloads(task_text: str, today: str, timeout: int = 30) -> list[str]:
    """Kick `brctl download` in parallel for every file the task touches.

    Best-effort: returns a list of paths that are STILL iCloud-locked
    after the warm-up window. The caller (run_task) can then choose to
    fail fast ("source files evicted from iCloud") instead of burning
    the whole 1200s budget on 20 failed read attempts.

    Walks:
      - every absolute .md path mentioned in the task (extracted by
        extract_absolute_md_paths)
      - every absolute folder mentioned in the task, recursively for
        any .md inside (transcript folders like HT102, transcript.lol
        dumps, etc.)
      - the inferred target note path
    Non-macOS platforms return an empty list (no iCloud).
    """
    if sys.platform != "darwin":
        return []
    brctl = shutil.which("brctl")
    if not brctl:
        return []

    candidates: set[Path] = set()
    for raw in extract_absolute_md_paths(task_text):
        p = Path(raw).expanduser()
        if p.is_file():
            candidates.add(p)
        elif p.is_dir():
            for child in p.rglob("*.md"):
                if child.is_file():
                    candidates.add(child)
    target = infer_target_note_path(task_text)
    if target:
        p = Path(target).expanduser()
        if p.is_file():
            candidates.add(p)

    if not candidates:
        return []

    # Fire all downloads in parallel. brctl download is async, so the
    # subprocess.run calls return quickly even for big files. We use
    # threads instead of processes — the only blocking work is the

    # brctl subprocess itself.
    import threading

    def _kick(path: Path) -> None:
        try:
            subprocess.run(
                [brctl, "download", str(path)],
                capture_output=True,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    threads = [threading.Thread(target=_kick, args=(p,), daemon=True) for p in candidates]
    for t in threads:
        t.start()
    # Give the daemon a chance to do the work. The 30s default is
    # generous for a single vault tree; preflight costs at most 30s +
    # the per-file poll in _ensure_icloud_downloaded when the LLM
    # later reads the file.
    deadline = time.time() + timeout
    for t in threads:
        remaining = max(0.1, deadline - time.time())
        t.join(timeout=remaining)
        if time.time() > deadline:
            break

    # Verify: which of the candidates are STILL EDEADLK after warm-up?
    still_locked: list[str] = []
    for p in candidates:
        try:
            with open(p, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            if _is_icloud_lock_error(exc):
                still_locked.append(str(p))
    return still_locked


# iCloud aggressively holds file locks on the daily note (Obsidian itself is
# constantly reading/writing it). On contended ticks the default 5-attempt
# backoff (0.4 + 0.8 + 1.6 + 3.2 + 6.4 = ~12s) is not enough and the tick
# logs `ERROR: read daily note failed: [Errno 11] Resource deadlock avoided`
# every 30s, producing a long string of useless tick cycles. Use a much longer
# retry on daily-note reads specifically: 10 attempts, 1s base, capped at 32s
# per backoff, with a small random jitter so two concurrent LaunchAgent ticks
# don't synchronize their retries and starve iCloud.
DAILY_NOTE_READ_ATTEMPTS = 10
DAILY_NOTE_READ_BASE_DELAY = 1.0
DAILY_NOTE_READ_MAX_DELAY = 32.0
DAILY_NOTE_WRITE_ATTEMPTS = 10


def _read_daily_note_with_retry(path: Path) -> str:
    """Read the daily note with a long, jittered retry budget for EDEADLK."""
    last: Exception | None = None
    for i in range(DAILY_NOTE_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            # Last attempt: no sleep, raise immediately on the way out.
            if i == DAILY_NOTE_READ_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** i), DAILY_NOTE_READ_MAX_DELAY)
            # Jitter ±20% so two simultaneous ticks don't retry in lockstep.
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None  # for type-checker
    raise last


def _write_daily_note_with_retry(path: Path, content: str) -> None:
    """Write the daily note with a long, jittered retry budget for EDEADLK.

    Mirror of `_read_daily_note_with_retry`. The 10-attempt budget is generous
    on purpose: under sustained iCloud contention this is the only retry loop
    standing between the worker and an exit-1 tick, so we accept a longer
    wait in exchange for not wasting a tick cycle on a recoverable error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for i in range(DAILY_NOTE_WRITE_ATTEMPTS):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last = exc
            if i == DAILY_NOTE_WRITE_ATTEMPTS - 1:
                break
            delay = min(DAILY_NOTE_READ_BASE_DELAY * (2 ** i), DAILY_NOTE_READ_MAX_DELAY)
            delay = delay * (0.8 + random.random() * 0.4)
            time.sleep(delay)
    assert last is not None
