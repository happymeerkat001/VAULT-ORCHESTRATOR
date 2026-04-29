# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read-only contract

This system NEVER performs destructive or mutating API calls on external services.
Allowed: GET, list, read, fetch.
Forbidden: delete, update, patch, send, modify — on any external API (Gmail, Calendar, etc.).

## Running scripts

```bash
# Run any ingest script directly
python3 ingest/hedy_sync.py
python3 ingest/briefing_sync.py
python3 ingest/mymemo_sync.py
python3 ingest/vision_sync.py

# CLI cross-vault transfer (keywords are positional args)
python3 cli/transfer_learning_to_neural.py --keywords "python" "AI" "LLM"
```

No build step, no test suite, no linter — pure stdlib Python 3.9+.

## Architecture

```
ingest/     Raw API fetchers. One file per source. Returns structured dicts.
process/    Local filtering/parsing via Python regex. No LLM calls here.
deliver/    Intentionally empty — all writes happen in-script via append_to_note().
cli/        One-shot manual trigger scripts.
```

### Pipeline flow

Each ingest script is self-contained: fetch → format → write. The `deliver/` directory exists for convention but output is handled by `append_to_note()` defined inside each ingest script.

### Shared patterns

Every ingest script uses these two idioms — match them when adding new sources:

- **`append_to_note(path, content)`** — idempotent append to a daily note. Always paired with a dedup check.
- **`get_existing_titles(note_path)`** — reads the note, extracts `### Title` headings via regex, prevents duplicate sections.
- Timezone filtering uses `zoneinfo.ZoneInfo` with UTC fallback for Python < 3.9 compatibility.

## Credentials

Two credential stores — do not conflate them:

| Store | Contents |
|-------|----------|
| `.env` (repo root, gitignored) | `MINIMAX_API_KEY`, `HEDY_API_KEY` |
| `~/.config/vault-orchestrator/google_credentials` | Google OAuth2 tokens |
| `~/.config/mymemo/credentials` | MyMemo JWT + auth0 cookie |
| `~/.config/anthropic/credentials` | Claude API key (vision_sync only) |

All `~/.config/` files are JSON, chmod 600.

## Obsidian vault path

```
~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator/
```

Use the lowercase `Neural-orchestrator` path consistently. Daily notes land at `Daily Notes/YYYY-MM-DD.md`, and `briefing_sync.py` writes the morning briefing section into that same note.

## Token minimization

LLM calls (MiniMax, Claude) are expensive. Always pre-filter in `process/` using Python regex/parsing before passing to any LLM. Never pass raw API responses directly to an LLM.

## Dependencies

Zero pip packages in `ingest/` and `process/`. Stdlib only: `urllib`, `json`, `pathlib`, `re`, `datetime`, `zoneinfo`.
