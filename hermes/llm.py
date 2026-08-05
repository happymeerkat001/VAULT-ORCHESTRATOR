"""MiniMax request, context-management, and failure-classification helpers."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from .tools import TOOLS

MINIMAX_URL = "https://api.minimaxi.chat/v1/chat/completions"
DEFAULT_MODEL = "MiniMax-M2.7"
CONTEXT_CHAR_BUDGET = 100_000

def configure(*, minimax_url: str, context_char_budget: int) -> None:
    globals().update(MINIMAX_URL=minimax_url, CONTEXT_CHAR_BUDGET=context_char_budget)



def _messages_total_chars(messages: list[dict]) -> int:
    """Sum the string length of every `content` field in the message list.

    Tool calls' `function.arguments` strings are also counted because
    long file paths or regex patterns the model sends can be sizable.
    Cheap O(n) scan; called once per iteration.
    """
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        tc = m.get("tool_calls") or []
        for call in tc:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                total += len(args)
    return total


def _trim_messages_for_context(messages: list[dict], budget_chars: int = CONTEXT_CHAR_BUDGET) -> int:
    """Replace the oldest trim-eligible content with a sentinel until under budget.

    Trim policy: keep
      - index 0 (system prompt)
      - index 1 (original user task)
      - the last _KEEP_RECENT_TURNS messages verbatim
    Walk the middle (indices 2 .. len - _KEEP_RECENT_TURNS - 1) from
    oldest to newest; for each message with a string `content`, replace
    it with _TRIM_SENTINEL. Tool calls' `function.arguments` strings are
    also replaced with "{}" to drop their contribution.

    Returns the number of messages trimmed. Idempotent: messages already
    trimmed (content == _TRIM_SENTINEL) are skipped, so calling twice is
    safe and a re-trim after a fresh tool result doesn't undo itself.

    The trim never deletes messages — only swaps content. The model
    still sees the full turn order (and the tool_call_ids needed to
    match tool results), so the API contract is preserved.
    """
    n = len(messages)
    if n <= _KEEP_RECENT_TURNS + 2:
        # Too few messages to bother trimming.
        return 0
    # Protected range: 0 (system), 1 (user task), and the last K turns.
    protect_end = n - _KEEP_RECENT_TURNS
    if protect_end <= 2:
        return 0
    trimmed = 0
    for idx in range(2, protect_end):
        m = messages[idx]
        c = m.get("content")
        if isinstance(c, str) and c != _TRIM_SENTINEL:
            m["content"] = _TRIM_SENTINEL
            trimmed += 1
        tc = m.get("tool_calls") or []
        for call in tc:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str) and args != "{}":
                fn["arguments"] = "{}"
    return trimmed


# ----------------------- LLM tool-use loop ---------------------------------

SYSTEM_PROMPT = (
    "You are Hermes, an autonomous task worker. The user added an unchecked "
    "task to today's daily note under '## Hermes-to-do 🪶'. Your job is to "
    "do that task end-to-end using the tools provided.\n\n"
    "Rules:\n"
    "1. A pre-task breakdown is created before you begin execution. Follow it "
    "as a working checklist, but adapt if tool results show a better path.\n"
    "2. Use tools to gather context, perform actions, and verify the result.\n"
    "3. You may READ and WRITE any file inside the vault when needed to "
    "complete the task. The only exception is the Hermes-to-do section of the "
    "daily note: the worker script manages that section state on your behalf.\n"
    "4. When the task is research, analysis, or synthesis, prefer writing a "
    "final markdown deliverable to 'Hermes Output/' using write_output_file. "
    "If the task explicitly asks you to update an existing note, you may write "
    "that note directly instead.\n"
    "5. After writing the deliverable or updating the target note, your final "
    "assistant message should be plain text: a 2-4 sentence summary of what you "
    "did, with the path(s) you wrote. Do not call any more tools after writing.\n"
    "6. NEVER add new items to the Hermes-to-do section. NEVER edit the "
    "daily note's Hermes-to-do checkbox state yourself. The worker script will "
    "mark the task done or restore it to open.\n"
    "7. If the task cannot be done with the available tools, do not pretend it "
    "succeeded. Return a brief explanation of what was attempted and why it "
    "failed. The worker script will leave the checkbox unchecked so it can be "
    "retried later.\n"
    "8. The daily note uses task states like `- [ ]` open, `- [~]` in progress "
    "(with `_(running)_` suffix), and `- [x]` done. You may see `_(running)_` "
    "markers while reading the vault; treat them as worker bookkeeping, not as "
    "content to edit directly.\n"
    "9. For research tasks that need up-to-date public information, use "
    "web_search to discover sources, then web_fetch to read specific pages. "
    "web_search is the DuckDuckGo HTML endpoint (no API key); web_fetch is "
    "a plain HTTP GET that strips HTML to text. Both are READ-ONLY — they "
    "cannot write to the vault or to any external service. Each call has a "
    "30s timeout and a per-page size cap. Cite the URLs you actually read in "
    "the deliverable so the user can verify.\n"
    "10. The user's task line may begin with an effort hint in square "
    "brackets, e.g. `[1800s]` to extend the per-task wall-clock budget for "
    "this single tick. If you see one, treat the task as high-effort and "
    "plan for up to that many seconds of total work; do not pad with idle "
    "loops to burn the budget.\n"
    "11. iCloud sync occasionally holds a file's lock when its local copy "
    "has been evicted (you will see this as errno 11 / errno 35). The "
    "worker auto-triggers a `brctl download` on the first such error and "
    "returns a soft \"(file locked by iCloud sync — download queued, "
    "retry this file later in the run)\" message if the lock persists. "
    "If you see that message, move on to other files in the run and "
    "circle back to the locked one in a later iteration — it will be "
    "local by then.\n"
    "12. The worker enforces a context budget on the message history. "
    "When the conversation grows past the budget, the oldest tool "
    "results are replaced with the literal string \"(result trimmed to "
    "fit context — re-read the file if needed)\". If you need the full "

    "content of a trimmed file, issue a fresh read_file (or list_directory "
    "+ search_files) to fetch it again. Do NOT assume the trimmed "
    "sentinel is the actual file content."
)


def call_minimax(
    messages: list[dict],
    api_key: str,
    tools: list[dict] | None = TOOLS,
    tool_choice: str | None = "auto",
    hard_timeout: int = 90,
    read_timeout: int = 60,
    retries: int = 2,
) -> dict:
    """Call the MiniMax chat completions endpoint with a hard wall-clock cap.

    `urllib.request.urlopen(req, timeout=60)` only honors the timeout on
    the *read* phase, not the *connect/handshake* phase. Observed: on
    sustained iCloud or wifi contention, the SSL handshake can hang for
    minutes, blocking the whole tool-use loop. We bound the call with a
    `Thread` + `join(timeout=hard_timeout)`; if the thread is still alive
    at the deadline we raise a TimeoutError. The thread itself keeps
    running until the OS-level socket times out, but the worker tick is
    unblocked and the in-flight call gets garbage-collected eventually.
    """
    import threading

    body_obj: dict = {
        "model": os.environ.get("HERMES_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "temperature": 0.2,
    }
    if tools is not None:
        body_obj["tools"] = tools
    if tool_choice is not None:
        body_obj["tool_choice"] = tool_choice
    body = json.dumps(body_obj).encode("utf-8")
    auth_value = "Bearer" + " " + api_key
    req = urllib.request.Request(
        MINIMAX_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_value,
        },
    )

    def _attempt() -> dict:
        result: dict | None = None
        error: BaseException | None = None

        def _runner() -> None:
            nonlocal result, error
            try:
                with urllib.request.urlopen(req, timeout=read_timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except BaseException as exc:  # noqa: BLE001 - we want to capture everything
                error = exc

        t = threading.Thread(target=_runner, daemon=True, name="minimax-call")
        t.start()
        t.join(timeout=hard_timeout)
        if t.is_alive():
            # The thread is still running. We can't safely kill it, but we can
            # stop waiting. The next iteration of the worker loop will start a
            # fresh thread; the daemon=True flag means Python won't block on
            # the orphaned thread at interpreter shutdown.
            raise TimeoutError("MiniMax call exceeded %ds hard timeout" % hard_timeout)
        if error is not None:
            if isinstance(error, urllib.error.HTTPError):
                detail = error.read().decode("utf-8", errors="replace")
                raise RuntimeError("MiniMax HTTP %s: %s" % (error.code, detail[:400])) from error
            raise RuntimeError("MiniMax call failed: %s" % error) from error
        assert result is not None
        return result

    # Timeouts (hard-timeout and socket read timeouts) are transient on
    # MiniMax under load; retry them with a short backoff instead of failing
    # the whole task. Non-timeout errors (HTTP 4xx/5xx, SSL, parse) are
    # raised immediately — retrying those wastes budget.
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _attempt()
        except (TimeoutError, RuntimeError) as exc:
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            if not is_timeout or attempt == retries:
                raise
            last_exc = exc
            delay = 2 * (attempt + 1)
            print("[worker] MiniMax timeout (attempt %d/%d), retrying in %ds: %s"
                  % (attempt + 1, retries + 1, delay, exc))
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def build_task_breakdown(
    task_text: str,
    today: str,
    max_seconds: int,
    max_iterations: int,
    api_key: str,
    explicit_md_paths: list[str],
    target_note_path: str,
) -> str:
    """Create a small execution checklist before starting work.

    This intentionally runs before the tool-use loop so large daily-note tasks
    are decomposed into manageable parts. It does not get tools and must not
    make claims about the vault contents; it is only an execution plan.
    """
    target_line = target_note_path or "(none detected)"
    paths_line = "\n".join("- " + p for p in explicit_md_paths) if explicit_md_paths else "(none)"
    messages = [
        {
            "role": "system",
            "content": (
                "You decompose one Hermes-to-do task into a practical execution "
                "checklist before any work begins. Return only a compact numbered "
                "list with 3-7 manageable parts. Do not use tools. Do not claim "
                "you have read files or completed anything. Include verification "
                "as the final part."
            ),
        },
        {
            "role": "user",
            "content": (
                "Today's date: %s\n"
                "Task: %s\n"
                "Per-task budget: %d seconds, %d LLM iterations.\n"
                "Explicit target note path: %s\n"
                "Markdown paths mentioned:\n%s\n\n"
                "Break this into smaller, manageable parts before execution."
            ) % (today, task_text, max_seconds, max_iterations, target_line, paths_line),
        },
    ]
    try:
        data = call_minimax(messages, api_key, tools=None, tool_choice=None)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
        if content:
            return content[:3000]
    except Exception:
        pass

    fallback = [
        "1. Identify the target deliverable and any explicitly named source or target notes.",
        "2. Read the relevant local notes and/or fetch the minimum needed external sources.",
        "3. Extract the key facts, decisions, quotes, and gaps relevant to the task.",
        "4. Update the target note directly, or write a focused Hermes Output deliverable if no target note is clear.",
        "5. Verify the written result answers the original task and points to the correct output path.",
    ]
    return "\n".join(fallback)

#       which the caller renders as `- [ ]` sub-bullets under the `try:` line.
#       Falls back to a generic split if the LLM call fails.

_FAILURE_TEMPLATES = {
    "iter_cap": (
        "iterations exceeded: model did not converge in {iter_limit} tool-use steps",
        "task too complex for one tick; try: split into smaller parts, raise [NNNi] budget, or pick a narrower scope",
    ),
    "wall_clock": (
        "timed out: {seconds}s wall clock",
        "task too long for one tick; try: split into parts, raise [NNNs] budget, or run research manually in Hermes Lab",
    ),
    "llm_network": (
        "LLM call failed: network/SSL error to api.minimaxi.chat",
        "transient network issue; try: wait 60s and re-run, or check api.minimaxi.chat status",
    ),
    "llm_http": (
        "LLM call failed: HTTP {http_code} from api.minimaxi.chat",
        "API rejected the request; try: rephrase the task, or check the daily note for malformed unicode",
    ),
    "llm_parse": (
        "LLM returned an unparseable response",
        "MiniMax model glitch; try: re-run the tick (the LLM may give a valid answer on retry)",
    ),
    "tool_error": (
        "tool error: {tool_name}",
        "an internal tool call failed; try: re-run, or simplify the task to avoid the failing tool",
    ),
    "no_writes": (
        "completed but wrote nothing to disk",
        "model finished without producing a deliverable; try: rephrase the task so the output file path is explicit",
    ),
    "icloud_lock": (
        "source files evicted from iCloud, download triggered",
        "re-run next tick (files should be local now), or open the folder in Finder to force the download",
    ),
}


def classify_failure(summary: str, iter_limit: int = 0, seconds: int = 0, http_code: int = 0) -> tuple[str, str, str]:
    """Map a `run_task` summary to a (category, reason, suggestion) tuple.

    The summary strings are produced by `run_task` (see the early-return
    paths above). Order of matching matters: more specific patterns first.
    """
    s = summary or ""
    # iCloud lock is a self-healing failure: the LLM never saw the
    # files, the brctl kicks are in flight, and a re-tick will land on
    # already-local bytes. Match it early so the sticky-failure counter
    # in process_one is skipped for this category.
    if "iCloud" in s and "locked" in s:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["icloud_lock"]
        return "icloud_lock", reason_t, suggestion_t
    m = re.search(r"timed out after (\d+)s", s)
    if m:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["wall_clock"]
        return "wall_clock", reason_t.format(seconds=m.group(1)), suggestion_t
    m = re.search(r"exceeded (\d+) iterations", s)
    if m:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["iter_cap"]
        return "iter_cap", reason_t.format(iter_limit=m.group(1)), suggestion_t
    m = re.search(r"LLM call failed:\s*MiniMax HTTP (\d+)(?::\s*(.*))?$", s, re.DOTALL)
    if m:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["llm_http"]
        reason = reason_t.format(http_code=m.group(1))
        # Surface the response body so the annotation tells us whether
        # the 400 was "context length exceeded" / "invalid request" /
        # "model overloaded" — three very different root causes. The
        # body is already capped to 400 chars at the call_minimax site
        # (urllib.error.HTTPError.read().decode()[:400]); we re-normalize
        # whitespace and cap to 200 chars here to keep the daily-note
        # annotation line readable.
        body = (m.group(2) or "").strip()
        if body:
            body = re.sub(r"\s+", " ", body)[:200]
            reason = "%s — %s" % (reason, body)
        return "llm_http", reason, suggestion_t
    if "SSL" in s or "ConnectionError" in s or "RemoteDisconnected" in s \
            or "Connection reset" in s or "Connection refused" in s \
            or "Connection aborted" in s or "URLError" in s \
            or "urlopen error" in s or "NewConnectionError" in s \
            or "SSLError" in s or "BadStatusLine" in s:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["llm_network"]
        return "llm_network", reason_t, suggestion_t
    if s.startswith("LLM call failed"):
        # Specific case first: a hard-timeout raised by call_minimax is a
        # network/transport issue, not a model-parsing issue. Classify as
        # llm_network so the suggestion points to retry/wait rather than
        # blaming the model.
        if "TimeoutError" in s or "hard timeout" in s or "exceeded" in s and "timeout" in s:
            reason_t, suggestion_t = _FAILURE_TEMPLATES["llm_network"]
            return "llm_network", reason_t, suggestion_t
        # Generic unrecognized LLM error: surface it as llm_parse so the
        # user sees the raw cause without us swallowing it.
        reason_t, suggestion_t = _FAILURE_TEMPLATES["llm_parse"]
        return "llm_parse", "%s: %s" % (reason_t, s.split(":", 1)[-1].strip()[:200]), suggestion_t
    if s.startswith("tool error"):
        tool_name = s.split(":", 1)[-1].strip().split()[0] if ":" in s else "unknown"
        reason_t, suggestion_t = _FAILURE_TEMPLATES["tool_error"]
        return "tool_error", reason_t.format(tool_name=tool_name), suggestion_t
    if "no deliverable" in s or "no output file written" in s:
        reason_t, suggestion_t = _FAILURE_TEMPLATES["no_writes"]
        return "no_writes", reason_t, suggestion_t
    # Default fallback
    return "other", s[:200], "try: re-run the tick, or simplify the task"


def suggest_split_subtasks(task_text: str, api_key: str) -> list[str]:
    """Ask MiniMax for 2-4 smaller child tasks that would accomplish the parent.

    Only called for "task too long" failures. The returned list is rendered
    as `- [ ]` sub-bullets under the `try:` line so the user can copy them
    into the queue. Returns an empty list if MiniMax is unavailable or
    refuses to produce a useful answer.
    """
    if not api_key:
        return []
    messages = [
        {
            "role": "system",
            "content": (
                "You help split an over-large research task into 2-4 smaller "
                "child tasks that, when run sequentially, would accomplish "
                "the parent. Return ONLY a numbered list, one task per line, "
                "no preamble, no commentary. Each child must be a single "
                "self-contained sentence starting with a verb (research, "

                "find, compare, write, list, summarize). Target length 10-25 "
                "words per child. Do not use tools."
            ),
        },
        {
            "role": "user",
            "content": (
                "Parent task that previously exceeded the worker's budget:\n\n"
                "%s\n\n"
                "Produce 2-4 child tasks."
            ) % task_text[:1500],
        },
    ]
    try:
        data = call_minimax(messages, api_key, tools=None, tool_choice=None)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
    except Exception:
        return []
    if not content:
        return []
    out: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading numbering like "1." or "1)" or "- "
        line = re.sub(r"^(\d+)[\.\)]\s+", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        if line and len(line) < 240:
            out.append(line)
        if len(out) >= 4:
            break
    return out[:4]
