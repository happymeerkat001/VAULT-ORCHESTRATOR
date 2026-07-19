# Obsidian-vault-orchestrator — Refactor & Optimization Audit (2026-07-18)

Fable audit; Codex implements. **This repo powers at least 7 live LaunchAgents** (hermes-worker, briefing.daily, hedy-sync, process-ingest, transcript-server, preflight, kanban) — every refactor must keep entry-point paths (`python3 cli/X.py`) working, since plists call them directly. Note: `vault-orchestrator` is a symlink to this repo — not a duplicate; nothing to merge.

## Verdict in one line

The architecture (ingest → process → deliver, read-only contract, sandboxed worker) is sound and CLAUDE.md is unusually accurate — the debt is concentrated in **one file**: `cli/hermes_worker.py` (2,237 lines) has grown into a complete homegrown agent runtime that deserves to become a package, plus shared iCloud/retry plumbing copy-pasted between modules.

## P0 — `cli/hermes_worker.py` is five subsystems in one file

51 functions covering: (a) iCloud-safe file I/O with retries, (b) a tool registry (read/list/search/web_fetch/web_search/move/write), (c) the Hermes-to-do state machine (`[ ]`/`[~]`/`[x]`/`[!]`, retry counts, failure annotation), (d) an LLM loop with context-trimming and failure classification, (e) CLI/lock/loop orchestration. Each is individually well-written; together they're unreviewable and untestable as a unit.

**Extraction plan (mechanical, low logic risk — keep `cli/hermes_worker.py` as thin entry point so the LaunchAgent path never changes):**
- `hermes/vaultio.py` — all `_read/_write_*_with_retry`, `_is_icloud_lock_error`, `_ensure_icloud_downloaded`, `_preflight_icloud_downloads`, `safe_path`/`safe_writable_path`. **This is the shared-infrastructure win:** `ingest/briefing_sync.py` (907 lines) has its own parallel retry helpers — converge both on this module.
- `hermes/tools.py` — `tool_*` functions + registry.
- `hermes/taskstate.py` — `extract_hermes_section`, `next_open_item`, `mark_*`, `get_retry_count`, `annotate_failure`, `_normalize_task_signature`, `_locate_task_line`. **Highest test value: pure text-transform logic guarding the daily note — unit-test this first** (crash-recovery re-pickup of `[~]`, suffix stripping, retry-count parsing).
- `hermes/llm.py` — `call_minimax`, `_trim_messages_for_context`, `classify_failure`, `suggest_split_subtasks`, `build_task_breakdown`.
- `cli/hermes_worker.py` keeps `main`, `process_one`, `run_task`, lock/loop.

Sequence: land the in-flight `fix/daily-note-rollover-catchup` work FIRST (5 modified CLI files + 2 untracked files sitting uncommitted since ~Jul 16 — commit or stash before any refactor touches `cli/`).

## P1 — Test infrastructure is broken at the invocation layer

`python3 -m pytest tests/` → "No module named pytest" with system Python 3.13. Tests exist (5 files) but there's no pinned environment: no `.venv/`, no `requirements.txt` at root, no `pyproject.toml`. Whatever env the LaunchAgents use is implicit.

- Add `pyproject.toml` (or root `requirements.txt`) + a repo venv; document `python3 -m venv .venv && .venv/bin/pip install -e .[test]` in CLAUDE.md.
- Add a `make test` / `scripts/test.sh` so agents (and you) run tests one way.
- Coverage today: ~750 test lines vs 10.5k code, all in ingest/naming areas — zero on `hermes_worker.py`, the most dangerous file (it edits the daily note). P0's `taskstate.py` extraction is what makes that testable.

## P2 — Consistency and duplication

1. **iCloud retry duplication** (see P0 vaultio) — `briefing_sync.py` and `hermes_worker.py` will drift on lock-error handling; one of them already has the newer fix.
2. **LLM stack:** worker pins `"MiniMax-M2.7"` inline (line ~1530); padsplit repo's summarizer uses `MiniMax-M2.5`; drafter uses Anthropic. Hoist to env (`HERMES_MODEL`) with the current value as default — one-line change, saves a code edit every model bump.
3. **`deliver/` is empty** — the README/CLAUDE architecture references ingest→process→deliver, but deliver has nothing. Either it's aspirational (delete the dir and fix docs) or outputs-to-`Hermes Output/` *is* deliver (say so in CLAUDE.md).
4. `cli/` mixes true CLIs with servers (`transcript_server.py`, `hermes_kanban_server.py`) and a one-off migration (`transfer_learning_to_neural.py`) — fine for now; only fix opportunistically. `scripts/migrate_z_ingestion.py` is untracked — commit or delete after the migration is confirmed done.

## P3 — Nits

- `_read_text_with_iCloud_retry` — camelCase 'iCloud' in a snake_case function name; rename during vaultio extraction.
- `.hermes/` untracked at root — gitignore it (same as angli-site).
- `tests/test_youtube_ingest_naming.py` untracked — commit it with the in-flight branch.

## Explicitly do NOT

- Do not replace the homegrown worker loop with a framework (Agent SDK, LangChain, etc.). The sandbox contract (writes only to `z.Ingestion/` + `Hermes Output/`, four-state checkboxes) is load-bearing safety logic tuned to your vault; a framework migration would re-derive it worse. Package it, don't replace it.
- Do not touch LaunchAgent plists or entry-point file paths during the refactor.
- Do not "clean up" the worker's iCloud retry ceilings/timeouts — they encode painful empirical iCloud behavior.

## Suggested Codex order

1. Land/commit the in-flight `fix/daily-note-rollover-catchup` work (with its untracked test file).
2. P1 env pinning (`pyproject.toml` + venv + test runner) — makes every later step verifiable.
3. P0 extraction, one module per commit, `taskstate.py` first **with new unit tests**, running the worker one manual tick (`python3 cli/hermes_worker.py`) after each commit.
4. P2.1 converge briefing_sync onto vaultio; P2.2–P2.4 and P3 opportunistically.
