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

# Archive pipeline (manual via shell orchestrator)
bash cli/run_archive.sh              # runs: archive_youtube.py → daily_note_youtube.py → scrape_notes.py

# Individual archive steps (if needed)
python3 cli/archive_youtube.py          # bare YouTube URLs from Untitled*.md → z.Ingestion/
python3 cli/daily_note_youtube.py       # bare YouTube URLs from Daily Notes/YYYY-MM-DD.md → z.Ingestion/
python3 cli/scrape_notes.py             # date-named notes + YouTube URLs + OCR images → z.Ingestion/
python3 cli/reprocess_youtube_stubs.py  # URL-only z.Ingestion stubs → normal titled transcript notes

# Transcript.lol integration
python3 cli/export_transcripts.py [--dry-run] [--output-dir <dir>]  # export completed Transcript.lol recordings
python3 cli/transcribe.py <URL> [--test-auth]                       # print transcript; Vimeo tries captions first
python3 cli/transcript.py <URL...> [--append-links-to-note <path>]  # save transcripts into z.Ingestion/
python3 cli/transcript_server.py                                    # local Chrome extension bridge on port 8765

# Post-processing
python3 scripts/process_ingest.py       # batch-OCR images in z.Ingestion/, upload to Imgur

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

### Important transcript examples

Use these commands when validating transcript behavior:

```bash
# Vimeo public sample: should print captions directly, no Transcript.lol recording
python3 -u cli/transcribe.py "https://vimeo.com/76979871"

# Vimeo fallback case: no captions, should fall back to Transcript.lol and fail fast on MEDIA_IMPORT_FAILED
python3 -u cli/transcribe.py "https://player.vimeo.com/video/1187098771?app_id=122963"

# Save a Vimeo transcript into z.Ingestion/ using captions-first logic
python3 cli/transcript.py "https://vimeo.com/76979871"

# iPhone share flow: root YYYY-MM-DD.md with bare YouTube URLs
python3 cli/scrape_notes.py --dry-run
python3 cli/scrape_notes.py

# Reprocess old URL-only ingest stubs
python3 cli/reprocess_youtube_stubs.py --dry-run
python3 cli/reprocess_youtube_stubs.py
```

### Chrome extension

The Chrome extension lives in `chrome-extension/` and requires `python3 cli/transcript_server.py` running on port 8765.

It adds two YouTube page buttons:

- **📝 Transcript.lol** — appends the bare YouTube URL to the vault root `YYYY-MM-DD.md`, matching the iPhone share flow processed by `python3 cli/scrape_notes.py` / `/obsidian`.
- **▶ YouTube Only** — calls `/transcript` for an immediate transcript save through the local server.

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
| `.env` (repo root, gitignored) | `MINIMAX_API_KEY`, `ANTHROPIC_BASE_URL`, `HEDY_AI_API_KEY`, `IMGUR_CLIENT_ID`, `FIREBASE_API_KEY`, `TRANSCRIPT_LOL_SPACE_ID`, `TRANSCRIPT_LOL_API_KEY`, `TRANSCRIPT_LOL_SUMMARY_PROMPT_ID`, `TRANSCRIPT_LOL_SUMMARY_TWEAK`, `Transcript.lol_Login`, `Transcript.lol_Password`, `TRANSCRIPT_LOL_AUTH_TOKEN`, `TRANSCRIPT_LOL_SESSION_COOKIE`, `GOOGLE_*` |
| `~/.config/vault-orchestrator/google_credentials` | Google OAuth2 tokens (refresh + access) |
| `~/.config/mymemo/credentials` | MyMemo JWT + auth0 cookie |
| `~/.config/anthropic/credentials` | Claude API key (vision_sync only) |

All `~/.config/` files are JSON, chmod 600. `ANTHROPIC_BASE_URL` points to MiniMax's Anthropic-compatible proxy. Transcript.lol auth supports multiple methods: `SPACE_ID + API_KEY` (preferred), `AUTH_TOKEN`, `SESSION_COOKIE`, or login/password flow with `FIREBASE_API_KEY`.

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
| `/transcript <url>` | `transcript.md` | Save media transcript as markdown into `z.Ingestion/`. YouTube prefers native captions before Transcript.lol fallback; Vimeo prefers yt-dlp captions before Transcript.lol fallback. No `*` prefix — run `/obsidian` after to add it. |
| `/obsidian` | `obsidian.md` | Process vault root `YYYY-MM-DD.md` files: copy to `z.Ingestion/*YYYY-MM-DD ingest.md`, archive originals to `processed/`, append wikilinks to matching daily notes. Also uploads embedded images to Imgur. |

Each command file contains only: a one-line description, a fenced bash block, and error guidance.

## Dependencies

Zero pip packages in `ingest/`, `process/`, `cli/`. Stdlib only: `urllib`, `json`, `pathlib`, `re`, `datetime`, `zoneinfo`.
