# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read-only contract

This system NEVER performs destructive or mutating API calls on external services.
Allowed: GET, list, read, fetch.
Forbidden: delete, update, patch, send, modify — on any external API (Gmail, Calendar, etc.).

## Running scripts

```bash
# Ingest scripts (automated or manual)
python3 ingest/hedy_sync.py
python3 ingest/briefing_sync.py
python3 ingest/mymemo_sync.py
python3 ingest/vision_sync.py

# Archive pipeline (manual, run in order)
python3 cli/archive_youtube.py          # archive bare YouTube URLs from Untitled*.md → z.Ingestion/
python3 cli/daily_note_youtube.py       # archive bare YouTube URLs from Daily Notes/YYYY-MM-DD.md → z.Ingestion/
python3 cli/scrape_notes.py             # archive date-named notes + OCR images → z.Ingestion/
python3 scripts/process_ingest.py       # batch-OCR images in z.Ingestion/, upload to Imgur

# Transcript ingestion
python3 cli/transcript.py <URL> [--output-dir <dir>] [--append-links-to-note <note_path>]

# Cross-vault transfer (keywords are positional args)
python3 cli/transfer_learning_to_neural.py --keywords "python" "AI" "LLM"

# Re-authorize Google OAuth2 (opens browser, captures redirect on localhost)
python3 cli/google_reauth.py
```

No build step, no test suite, no linter — pure stdlib Python 3.9+.

## Architecture

```
ingest/     Raw API fetchers. One file per source. Returns structured dicts.
process/    Local filtering/parsing via Python regex. No LLM calls here.
deliver/    Intentionally empty — all writes happen in-script via append_to_note().
cli/        One-shot manual trigger scripts.
scripts/    Post-processing (process_ingest.py: image OCR + Imgur archival).
```

### Pipeline flow

Each ingest script is self-contained: fetch → format → write. The `deliver/` directory exists for convention but output is handled by `append_to_note()` defined inside each ingest script.

Archive pipeline writes to `z.Ingestion/` in the vault. `process_ingest.py` runs after archival to OCR embedded images and replace local/remote links with Imgur-hosted URLs.

### Shared patterns

Every ingest script uses these idioms — match them when adding new sources:

- **`append_to_note(path, content)`** — idempotent append to a daily note. Always paired with a dedup check.
- **`get_existing_titles(note_path)`** — reads the note, extracts `### Title` headings via regex, prevents duplicate sections.
- **iCloud retry**: 5× loop with 1s delay on `OSError` (EDEADLK) when writing to iCloud paths under launchd.
- **Timezone filtering**: `zoneinfo.ZoneInfo` with UTC fallback for Python < 3.9 compatibility.
- **Keyword linking**: `KEYWORD_MAP` in `ingest/hedy_common.py` auto-inserts Obsidian wikilinks (e.g. `Python` → `[[Python]]`). Extend it there.

## Credentials

| Store | Contents |
|-------|----------|
| `.env` (repo root, gitignored) | `MINIMAX_API_KEY` for `briefing_sync.py`, `ANTHROPIC_BASE_URL`, `HEDY_AI_API_KEY`, `IMGUR_CLIENT_ID`, `FIREBASE_API_KEY`, `TRANSCRIPT_LOL_SUMMARY_PROMPT_ID`, `TRANSCRIPT_LOL_SUMMARY_TWEAK`, `Transcript.lol_Login`, `Transcript.lol_Password`, `GOOGLE_*` |
| `~/.config/vault-orchestrator/google_credentials` | Google OAuth2 tokens (refresh + access) |
| `~/.config/mymemo/credentials` | MyMemo JWT + auth0 cookie |
| `~/.config/anthropic/credentials` | Claude API key (vision_sync only) |

All `~/.config/` files are JSON, chmod 600. `ANTHROPIC_BASE_URL` points to MiniMax's Anthropic-compatible proxy — not the real Anthropic endpoint.

## Scheduling

Daily pipeline runs at 5:47 AM via macOS LaunchAgent:
- Plist: `~/Library/LaunchAgents/com.leon.briefing.daily.plist`
- Entrypoint: `~/.claude/scripts/run-briefing.sh` → `briefing_sync.py`
- Logs: `~/.claude/logs/launchd-briefing.*.log`

Scripts must tolerate `EDEADLK` from iCloud file locks (see retry pattern above).

## Obsidian vault path

```
~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/
```

Use `AI-Vault` consistently. Daily notes: `Daily Notes/YYYY-MM-DD.md`. Ingestion archive: `z.Ingestion/`. Processed source notes move to `processed/`.

## Token minimization

LLM calls (MiniMax, Claude) are expensive. Always pre-filter in `process/` using Python regex/parsing before passing to any LLM. Never pass raw API responses directly to an LLM.

## Slash commands (.claude/commands/)

Three project slash commands live in `.claude/commands/`. Recreate them if missing:

| Command | File | Invokes |
|---------|------|---------|
| `/hedy` | `hedy.md` | Fetch Hedy API → write formatted items to daily note. Project-scoped (not global). |
| `/transcript <url>` | `transcript.md` | Save YouTube/media transcript as markdown into `z.Ingestion/`. Use transcript.lol prompt summaries first, then captions, then `transcript.lol` transcript fallback. No `*` prefix — run `/obsidian` after to add it. |
| `/obsidian` | `obsidian.md` | Process vault root `YYYY-MM-DD.md` files: copy to `z.Ingestion/*YYYY-MM-DD ingest.md`, archive originals to `processed/`, append wikilinks to matching daily notes. Also uploads embedded images to Imgur. |

Each command file contains only: a one-line description, a fenced bash block, and error guidance.

## Dependencies

Zero pip packages in `ingest/`, `process/`, `cli/`. Stdlib only: `urllib`, `json`, `pathlib`, `re`, `datetime`, `zoneinfo`.
