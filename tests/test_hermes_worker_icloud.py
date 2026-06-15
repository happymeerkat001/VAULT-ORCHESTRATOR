"""Unit tests for the iCloud EDEADLK self-heal layer in cli/hermes_worker.py.

The production failure mode that motivated this code:

    A read_file tool call against an iCloud-evicted file returns
    "Resource deadlock avoided" (errno 11) on every attempt. The default
    5-attempt / 0.4s-base retry budget gives up before iCloud releases
    the lock, so the LLM aborts. The fix is two layers: (1) shell out
    to `brctl download` to ask iCloud to materialize the file on the
    first lock error, and (2) extend the retry budget to match the
    daily-note helper. Plus a preflight that warms all source files in
    parallel before the LLM loop starts.

These tests fake the EDEADLK with a side-effect counter on a real temp
file. They do NOT shell out to brctl (subprocess.run is patched) and
they do NOT rely on the host being macOS — the helpers short-circuit
to no-ops off Darwin so the test runs on any platform.
"""

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import hermes_worker  # noqa: E402


def _make_edeadlk_exc():
    """Build an OSError that matches `_is_icloud_lock_error` (errno 11)."""
    return OSError(errno.EDEADLK, "Resource deadlock avoided")


def _make_eagain_exc():
    return OSError(errno.EAGAIN, "Resource temporarily unavailable")


class IsICloudLockErrorTests(unittest.TestCase):
    def test_matches_edeadlk(self):
        self.assertTrue(hermes_worker._is_icloud_lock_error(_make_edeadlk_exc()))

    def test_matches_eagain(self):
        self.assertTrue(hermes_worker._is_icloud_lock_error(_make_eagain_exc()))

    def test_matches_message_only(self):
        # Some wrapper exceptions don't carry errno but keep the message.
        exc = OSError("[Errno 11] Resource deadlock avoided")
        self.assertTrue(hermes_worker._is_icloud_lock_error(exc))

    def test_does_not_match_permission_error(self):
        self.assertFalse(hermes_worker._is_icloud_lock_error(PermissionError(13, "denied")))

    def test_does_not_match_file_not_found(self):
        self.assertFalse(hermes_worker._is_icloud_lock_error(FileNotFoundError(2, "no")))


class EnsureICloudDownloadedTests(unittest.TestCase):
    def test_noop_off_darwin(self):
        with tempfile.NamedTemporaryFile() as tf:
            with mock.patch.object(hermes_worker.sys, "platform", "linux"):
                ok = hermes_worker._ensure_icloud_downloaded(Path(tf.name))
        self.assertTrue(ok)

    def test_calls_brctl_then_succeeds(self):
        # The poll loop's first open() should succeed (file exists, no
        # EDEADLK injected) so the helper returns True and brctl was
        # called exactly once up front. Use `shutil.which` returning
        # just the bare command name so the first argv matches.
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = Path(tf.name)
        try:
            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value="brctl"), \
                 mock.patch.object(hermes_worker.subprocess, "run") as run_mock:
                ok = hermes_worker._ensure_icloud_downloaded(path)
            self.assertTrue(ok)
            # The first call is the upfront brctl kick; later ones are
            # optional re-kicks at iterations 1 and 3. The first call
            # must be present.
            first = run_mock.call_args_list[0]
            self.assertEqual(first.args[0][0], "brctl")
            self.assertEqual(first.args[0][1], "download")
            self.assertEqual(first.args[0][2], str(path))
        finally:
            path.unlink(missing_ok=True)

    def test_persistent_lock_returns_false(self):
        # Force the open() inside the poll loop to keep raising EDEADLK
        # so the helper exhausts its budget and returns False.
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = Path(tf.name)
        try:
            edeadlk = _make_edeadlk_exc()
            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value="/usr/bin/brctl"), \
                 mock.patch.object(hermes_worker.subprocess, "run"), \
                 mock.patch("builtins.open", side_effect=edeadlk):
                ok = hermes_worker._ensure_icloud_downloaded(path, attempts=2)
            self.assertFalse(ok)
        finally:
            path.unlink(missing_ok=True)

    def test_brctl_missing_returns_false(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = Path(tf.name)
        try:
            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value=None):
                ok = hermes_worker._ensure_icloud_downloaded(path)
            self.assertFalse(ok)
        finally:
            path.unlink(missing_ok=True)


class ReadTextWithICloudRetryTests(unittest.TestCase):
    def test_first_edeadlk_triggers_brctl_then_succeeds(self):
        # Simulate the production sequence: first read_text raises
        # EDEADLK, the helper shells out to brctl, subsequent reads
        # succeed. We assert that brctl was called and the file was
        # returned.
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("hello from iCloud")
            path = Path(tf.name)
        try:
            real_read_text = Path.read_text
            call_count = {"n": 0}

            def fake_read_text(self, *a, **kw):
                if self == path and call_count["n"] < 2:
                    call_count["n"] += 1
                    raise _make_edeadlk_exc()
                return real_read_text(self, *a, **kw)

            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value="brctl"), \
                 mock.patch.object(hermes_worker.subprocess, "run") as run_mock, \
                 mock.patch.object(Path, "read_text", fake_read_text), \
                 mock.patch("builtins.open") as open_mock, \
                 mock.patch.object(hermes_worker.time, "sleep"):
                # First open() in the poll loop of _ensure_icloud_downloaded
                # also needs to succeed (the loop reads 1 byte to detect
                # the lock is gone). Use a context manager mock.
                open_mock.return_value.__enter__.return_value.read.return_value = b"h"
                result = hermes_worker._read_text_with_iCloud_retry(path)

            self.assertIn("hello from iCloud", result)
            # brctl was kicked at least once.
            brctl_calls = [c for c in run_mock.call_args_list
                           if c.args and list(c.args[0][:2]) == ["brctl", "download"]]
            self.assertGreaterEqual(len(brctl_calls), 1)
        finally:
            path.unlink(missing_ok=True)

    def test_persistent_edeadlk_raises_after_budget(self):
        # All reads EDEADLK. Helper exhausts the 10-attempt budget and
        # re-raises the last EDEADLK to the caller. This is the failure
        # case that tool_read_file catches and converts to the soft
        # sentinel.
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("never read")
            path = Path(tf.name)
        try:
            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value="/usr/bin/brctl"), \
                 mock.patch.object(hermes_worker.subprocess, "run"), \
                 mock.patch.object(Path, "read_text", side_effect=_make_edeadlk_exc()), \
                 mock.patch("builtins.open", side_effect=_make_edeadlk_exc()):
                with mock.patch.object(hermes_worker.time, "sleep"):  # speed up test
                    with self.assertRaises(OSError) as ctx:
                        hermes_worker._read_text_with_iCloud_retry(path)
            self.assertEqual(ctx.exception.errno, errno.EDEADLK)
        finally:
            path.unlink(missing_ok=True)


class ToolReadFileSoftFallbackTests(unittest.TestCase):
    def test_soft_sentinel_returned_on_persistent_lock(self):
        # tool_read_file must return the sentinel string, NOT raise, so
        # the LLM can keep working on other files in the run.
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write("content")
            path = Path(tf.name)
        try:
            with mock.patch.object(hermes_worker, "_read_text_with_iCloud_retry",
                                   side_effect=_make_edeadlk_exc()):
                # safe_path() requires the path to live under VAULT_ROOT;
                # mock safe_path to return our temp file directly.
                with mock.patch.object(hermes_worker, "safe_path", return_value=path):
                    result = hermes_worker.tool_read_file({"path": str(path)})
            self.assertTrue(result.startswith(hermes_worker._ICLOUD_LOCK_SENTINEL))
        finally:
            path.unlink(missing_ok=True)

    def test_real_read_still_works(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write("alpha\nbeta")
            path = Path(tf.name)
        try:
            with mock.patch.object(hermes_worker, "_read_text_with_iCloud_retry",
                                   return_value="alpha\nbeta"):
                with mock.patch.object(hermes_worker, "safe_path", return_value=path):
                    result = hermes_worker.tool_read_file({"path": str(path)})
            self.assertEqual(result, "alpha\nbeta")
        finally:
            path.unlink(missing_ok=True)


class ClassifyFailureICloudLockTests(unittest.TestCase):
    def test_icloud_lock_category(self):
        cat, reason, suggestion = hermes_worker.classify_failure(
            "iCloud source files still locked after preflight: foo.md, bar.md"
        )
        self.assertEqual(cat, "icloud_lock")
        self.assertIn("iCloud", reason)
        self.assertIn("re-run", suggestion)

    def test_icloud_lock_category_short_form(self):
        # Should also catch the wording from the tool-read-file soft fallback
        # path (the sentinel is returned, not raised, so this is defense in
        # depth in case a future caller bubbles it up).
        cat, _, _ = hermes_worker.classify_failure(
            "(file locked by iCloud sync — download queued)"
        )
        self.assertEqual(cat, "icloud_lock")

    def test_existing_categories_unaffected(self):
        # The new branch must come BEFORE the wall_clock/iter_cap regex,
        # so a stuck-on-iCloud summary is not mis-classified.
        cat, _, _ = hermes_worker.classify_failure("timed out after 180s")
        self.assertEqual(cat, "wall_clock")
        cat, _, _ = hermes_worker.classify_failure("exceeded 20 iterations")
        self.assertEqual(cat, "iter_cap")


class PreflightICloudDownloadsTests(unittest.TestCase):
    def test_noop_without_explicit_paths(self):
        # A task text with no absolute .md paths and no target note
        # should short-circuit to an empty list without touching brctl.
        with mock.patch.object(hermes_worker, "extract_absolute_md_paths",
                               return_value=[]), \
             mock.patch.object(hermes_worker, "infer_target_note_path",
                               return_value=""), \
             mock.patch.object(hermes_worker.subprocess, "run") as run_mock, \
             mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
             mock.patch.object(hermes_worker.shutil, "which", return_value="/usr/bin/brctl"):
            result = hermes_worker._preflight_icloud_downloads(
                "do something without source paths", "2026-06-12", timeout=1
            )
        self.assertEqual(result, [])
        self.assertEqual(run_mock.call_count, 0)

    def test_noop_off_darwin(self):
        with mock.patch.object(hermes_worker.sys, "platform", "linux"):
            result = hermes_worker._preflight_icloud_downloads(
                "task with /some/path.md", "2026-06-12"
            )
        self.assertEqual(result, [])

    def test_still_locked_paths_are_returned(self):
        # Two source files; both stay EDEADLK after warm-up. The helper
        # should return their paths so run_task can fail fast.
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "one.md"
            p2 = Path(tmp) / "two.md"
            p1.write_text("a")
            p2.write_text("b")
            task_text = "process %s and %s" % (p1, p2)
            with mock.patch.object(hermes_worker.sys, "platform", "darwin"), \
                 mock.patch.object(hermes_worker.shutil, "which", return_value="/usr/bin/brctl"), \
                 mock.patch.object(hermes_worker.subprocess, "run"), \
                 mock.patch.object(hermes_worker.time, "sleep"), \
                 mock.patch("builtins.open", side_effect=_make_edeadlk_exc()):
                still = hermes_worker._preflight_icloud_downloads(
                    task_text, "2026-06-12", timeout=0  # skip the kick phase entirely
                )
        self.assertEqual(len(still), 2)
        self.assertTrue(any(p1.name in s for s in still))
        self.assertTrue(any(p2.name in s for s in still))


# ------------------------ context budget -------------------------------------

class ClassifyFailureHTTPBodyTests(unittest.TestCase):
    """The 400-detail surfacing from addition #6.

    call_minimax already captures urllib's error body (capped to 400 chars)
    and embeds it in the RuntimeError. classify_failure used to drop the
    body and report only the code, so a 'context length exceeded' 400 and
    a 'malformed input' 400 looked identical. The fix is to capture the
    body group from the summary and append it to the reason string so
    the daily-note annotation can tell the two apart.
    """

    def test_context_length_exceeded_body_surfaced(self):
        body = '{"error":{"message":"context length exceeded: max 32000 tokens","type":"invalid_request_error"}}'
        cat, reason, _ = hermes_worker.classify_failure("LLM call failed: MiniMax HTTP 400: " + body)
        self.assertEqual(cat, "llm_http")
        self.assertIn("400", reason)
        self.assertIn("context length exceeded", reason)

    def test_malformed_input_body_surfaced(self):
        body = '{"error":{"message":"invalid request: tool schema mismatch","type":"invalid_request_error"}}'
        cat, reason, _ = hermes_worker.classify_failure("LLM call failed: MiniMax HTTP 400: " + body)
        self.assertEqual(cat, "llm_http")
        self.assertIn("tool schema mismatch", reason)

    def test_body_whitespace_normalized(self):
        # The body may have embedded newlines / many spaces. The
        # classifier must collapse them so the annotation line stays
        # one readable line.
        body = "context\n\n length    exceeded: max\n32000"
        _, reason, _ = hermes_worker.classify_failure("LLM call failed: MiniMax HTTP 400: " + body)
        self.assertNotIn("\n", reason)
        self.assertNotIn("    ", reason)
        self.assertIn("context length exceeded: max 32000", reason)

    def test_body_capped_at_200_chars(self):
        # A verbose error body must not blow out the annotation line.
        body = "X" * 1000
        _, reason, _ = hermes_worker.classify_failure("LLM call failed: MiniMax HTTP 400: " + body)
        # Reason is "LLM call failed: HTTP 400 from api.minimaxi.chat — XXX..."
        # The body slice is 200 chars; verify it's bounded.
        body_part = reason.split(" — ", 1)[-1]
        self.assertLessEqual(len(body_part), 200)

    def test_empty_body_falls_back_cleanly(self):
        # The old regex matched "MiniMax HTTP N" with no body. The new
        # regex must still classify that case as llm_http with no
        # dangling " — " separator.
        cat, reason, _ = hermes_worker.classify_failure("LLM call failed: MiniMax HTTP 500")
        self.assertEqual(cat, "llm_http")
        self.assertIn("500", reason)
        self.assertFalse(reason.endswith(" — "))


class ReadFileClampTests(unittest.TestCase):
    """The 300-line hard ceiling on read_file (addition #7a)."""

    def setUp(self):
        # Build a fake 500-line file in memory.
        self.lines = ["line %d" % i for i in range(500)]

    def _read_with_limit(self, requested):
        fake_path = Path("/tmp/fake.md")
        with mock.patch.object(hermes_worker, "_read_text_with_iCloud_retry",
                               return_value="\n".join(self.lines)), \
             mock.patch.object(hermes_worker, "safe_path", return_value=fake_path), \
             mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "is_dir", return_value=False):
            return hermes_worker.tool_read_file({"path": "/tmp/fake.md",
                                                 "max_lines": requested})

    def test_clamps_requested_above_ceiling(self):
        result = self._read_with_limit(10000)
        # Should be capped to MAX_READ_FILE_LINES (300) lines, not 10000.
        result_lines = result.splitlines()
        # The last line is the truncation notice, so 300 content + 1 notice.
        self.assertEqual(len(result_lines), hermes_worker.MAX_READ_FILE_LINES + 1)
        self.assertIn("truncated at 300", result_lines[-1])

    def test_requested_within_ceiling_passes_through(self):
        result = self._read_with_limit(50)
        result_lines = result.splitlines()
        # 50 content lines + 1 truncation notice (because the file has
        # 500 lines total and we asked for fewer).
        self.assertEqual(len(result_lines), 51)
        self.assertIn("truncated at 50", result_lines[-1])

    def test_default_limit_200_not_clamped(self):
        # Default behavior is unchanged: 200 lines.
        result = self._read_with_limit(200)
        result_lines = result.splitlines()
        # 200 content + 1 truncation notice.
        self.assertEqual(len(result_lines), 201)
        self.assertIn("truncated at 200", result_lines[-1])

    def test_ceiling_constant_is_300(self):
        # The clamp ceiling is part of the worker's external contract
        # (declared in the tool schema description). Don't change it
        # without bumping the system prompt and skill doc.
        self.assertEqual(hermes_worker.MAX_READ_FILE_LINES, 300)


class ContextTrimGuardTests(unittest.TestCase):
    """The 100k-char context budget guard (addition #7b)."""

    def _make_messages(self, n_turns: int, content_size: int) -> list[dict]:
        # Mimic the run_task shape: [system, user_task, then alternating
        # assistant-with-tool-calls and tool-result turns].
        msgs = [
            {"role": "system", "content": "S" * 200},
            {"role": "user", "content": "U" * 200},
        ]
        for i in range(n_turns):
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_%d" % i,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "A" * content_size},
                }],
            })
            msgs.append({
                "role": "tool",
                "tool_call_id": "call_%d" % i,
                "name": "read_file",
                "content": "T" * content_size,
            })
        return msgs

    def test_total_chars_sums_content_and_args(self):
        msgs = self._make_messages(n_turns=3, content_size=100)
        # 2*200 (system+user) + 3*(100 args + 100 tool content) = 1000
        self.assertEqual(hermes_worker._messages_total_chars(msgs), 1000)

    def test_no_trim_when_too_few_messages(self):
        # The trim function trims whenever message count > KEEP_RECENT+2,
        # regardless of total size. Below that threshold, the protected
        # zone covers everything and no trim happens.
        msgs = self._make_messages(n_turns=1, content_size=10_000)
        n = hermes_worker._trim_messages_for_context(msgs, budget_chars=10)
        self.assertEqual(n, 0)
        # All content preserved.
        self.assertEqual(msgs[-1]["content"], "T" * 10_000)

    def test_no_trim_for_short_conversations(self):
        # 1 turn = 4 messages (system, user, assistant+tool_call, tool).
        # KEEP_RECENT=4 + protected 2 = 6; n=4 <= 6 → no trim.
        msgs = self._make_messages(n_turns=1, content_size=10_000)
        n = hermes_worker._trim_messages_for_context(msgs, budget_chars=10)
        self.assertEqual(n, 0)

    def test_trim_activates_above_keep_recent_threshold(self):
        # 3 turns = 8 messages. protect_end = 8-4 = 4. Trims indices 2..3.
        # Index 2 is assistant (content=None, args="A"*100 → "{}", no
        # content-count but args got replaced). Index 3 is tool
        # (content="T"*100 → sentinel, +1 count). Total content count: 1.
        # The function only counts content swaps, not args swaps.
        msgs = self._make_messages(n_turns=3, content_size=100)
        n = hermes_worker._trim_messages_for_context(msgs, budget_chars=10_000)
        self.assertEqual(n, 1)
        # Index 2's tool_call args got replaced (side effect, not counted).
        for call in msgs[2].get("tool_calls") or []:
            self.assertEqual(call["function"]["arguments"], "{}")
        # Index 3's content got replaced.
        self.assertEqual(msgs[3]["content"], hermes_worker._TRIM_SENTINEL)
        # Indices 0, 1, 4..7 untouched.
        for idx in [0, 1, 4, 5, 6, 7]:
            if isinstance(msgs[idx].get("content"), str):
                self.assertNotEqual(msgs[idx]["content"], hermes_worker._TRIM_SENTINEL)

    def test_trim_replaces_oldest_tool_turns(self):
        # 10 turns, each 1000 chars of tool content. Budget = 5000. The
        # protected zone is 0 (system) + 1 (user) + last 4 messages.
        # That leaves 16 - 4 = 12 trim-eligible messages (8 assistant
        # with tool_calls and 4 tool results among them; but the
        # trimmer walks by message index, not by role).
        msgs = self._make_messages(n_turns=10, content_size=1000)
        budget = 5_000
        n = hermes_worker._trim_messages_for_context(msgs, budget_chars=budget)
        self.assertGreater(n, 0)
        # System + user still untouched.
        self.assertNotEqual(msgs[0]["content"], hermes_worker._TRIM_SENTINEL)
        self.assertNotEqual(msgs[1]["content"], hermes_worker._TRIM_SENTINEL)
        # Last 4 messages still untouched.
        for idx in (-1, -2, -3, -4):
            m = msgs[idx]
            if isinstance(m.get("content"), str):
                self.assertNotEqual(m["content"], hermes_worker._TRIM_SENTINEL)
        # Middle messages all trimmed to the sentinel.
        # _KEEP_RECENT_TURNS = 4, n = 22 messages (system+user+10*2).
        # protected: [0,1] + last 4 = indices 0,1,18,19,20,21.
        # Trimmed: indices 2..17.
        for idx in range(2, 18):
            m = msgs[idx]
            if isinstance(m.get("content"), str):
                self.assertEqual(m["content"], hermes_worker._TRIM_SENTINEL)
            for call in m.get("tool_calls") or []:
                self.assertEqual(call["function"]["arguments"], "{}")

    def test_trim_is_idempotent(self):
        msgs = self._make_messages(n_turns=10, content_size=1000)
        first = hermes_worker._trim_messages_for_context(msgs, budget_chars=5_000)
        second = hermes_worker._trim_messages_for_context(msgs, budget_chars=5_000)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_trim_noop_when_too_few_messages(self):
        # Only system + user + 1 tool turn. protect_end <= 2, no trim.
        msgs = [
            {"role": "system", "content": "S" * 1000},
            {"role": "user", "content": "U" * 1000},
            {"role": "tool", "tool_call_id": "x", "name": "read_file", "content": "T" * 1000},
        ]
        n = hermes_worker._trim_messages_for_context(msgs, budget_chars=100)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
