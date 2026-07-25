# vault-orchestrator

Python utilities for pulling data from external services, processing it locally, and writing formatted markdown into an Obsidian vault.

## Design principles

- **Read-only by default.** External systems are treated as sources; the main write target is the vault.
- **Minimal dependencies.** Core workflows use Python and simple local config.
- **Token-efficient.** Data is filtered and formatted locally before any LLM call.
- **Composable.** Ingest, process, and export steps are separated so they can run independently.

## Folder structure

```text
ingest/     One file per external data source. Fetches raw API data.
process/    Pure Python filtering, parsing, and formatting.
deliver/    Writes final markdown to the Obsidian vault path.
cli/        Manual trigger scripts for one-shot runs.
scripts/    Vault cleanup and post-processing helpers.
```

## Main workflows

| Script | Source | Output |
|--------|--------|--------|
| `ingest/briefing_sync.py` | Google Calendar + Gmail + MiniMax | Creates or updates `Daily Notes/YYYY-MM-DD.md` |
| `ingest/hedy_sync.py` | Hedy AI sessions API | Writes recaps + action items to `z.Ingestion/Hedy-AI/YYYY-MM-DD.md` |
| `ingest/mymemo_sync.py` | MyMemo AI API | Appends podcast digests to `Daily Notes/YYYY-MM-DD.md` |
| `ingest/vision_sync.py` | Claude Vision API | OCR images from vault, uploads to Imgur |
| `ingest/weekly_housekeep_propose.py` | Local markdown scan | PR-style proposals in `Hermes Output/` + Daily Note addendum (never mutates notes by itself) |
| `cli/export_transcripts.py` | Transcript.lol recordings | Writes notes into `z.Ingestion/` and appends links into today's daily note |
| `cli/transcribe.py` | Single media URL | Prints transcript text to stdout |
| `cli/transcript.py` | One or more media URLs | Saves transcript markdown into `z.Ingestion/` |
| `cli/scrape_notes.py` | Root-level `YYYY-MM-DD.md` notes | Archives note text/OCR and routes bare YouTube URLs into transcript saves |
| `cli/archive_youtube.py` | Bare YouTube URLs in `Untitled*.md` / `New Note*.md` | Fetches transcripts → `z.Ingestion/` |
| `cli/daily_note_youtube.py` | Bare YouTube URLs in `Daily Notes/YYYY-MM-DD.md` | Fetches transcripts → `z.Ingestion/` |
| `cli/reprocess_youtube_stubs.py` | URL-only stubs in `z.Ingestion/` | Rewrites stubs as full titled transcript notes |
| `scripts/process_ingest.py` | Images embedded in `z.Ingestion/` | OCR + Imgur upload, rewrites local paths |
| `cli/transfer_learning_to_neural.py` | AI-Vault notes matching keywords | Copies matching notes to a second vault |

## Setup

### 1. Google + MiniMax credentials

```sh
mkdir -p ~/.config/vault-orchestrator
cat > ~/.config/vault-orchestrator/google_credentials <<'EOF'
{
  "minimax_api_key": "<MINIMAX_API_KEY>",
  "google_client_id": "<GOOGLE_CLIENT_ID>",
  "google_client_secret": "<GOOGLE_CLIENT_SECRET>",
  "google_redirect_uri": "<GOOGLE_REDIRECT_URI>",
  "google_refresh_token": "<GOOGLE_REFRESH_TOKEN>"
}
EOF
chmod 600 ~/.config/vault-orchestrator/google_credentials
```

### 2. Transcript.lol credentials

`cli/transcribe.py` and `cli/export_transcripts.py` read Transcript.lol auth from repo-root `.env` or matching process environment variables.

Minimum setup:

```sh
cat > .env <<'EOF'
TRANSCRIPT_LOL_SPACE_ID="your-space-id"
TRANSCRIPT_LOL_API_KEY="your-api-key"
EOF
```

Alternative auth is also supported:

- `TRANSCRIPT_LOL_AUTH_TOKEN`
- `TRANSCRIPT_LOL_SESSION_COOKIE`
- `Transcript.lol_Login` + `Transcript.lol_Password` with `FIREBASE_API_KEY`
- `Transcript.lol_Login` + `Transcript.lol_Password` with Playwright-installed browser login fallback
- `TRANSCRIPT_LOL_SPACE_NAME` to resolve a workspace by name instead of hard-coding the space id

Browser-login fallback install:

```sh
python3 -m pip install playwright
python3 -m playwright install chromium
```

Quick auth check:

```sh
python3 cli/transcribe.py --test-auth
```

### 3. Scheduling

Four LaunchAgents run automatically on macOS:

| Plist | Trigger | Runs |
|-------|---------|------|
| `com.leon.briefing.daily` | 5:47 AM + every 30 min + at load | `run-briefing.sh` → `python3 cli/daily_briefing_catchup.py` (no-op before 05:45 or once briefing exists; reauth via `cli/google_reauth.py` if needed) |
| `com.ang.yt-archive` | Vault file change + every 60s | `run_archive.sh` → `archive_youtube.py` + `daily_note_youtube.py` + `scrape_notes.py` |
| `com.leon.process-ingest` | Vault file change + every 30s | `scripts/process_ingest.py --apply` |
| `com.leon.transcript-server` | Always-on (KeepAlive) | `cli/transcript_server.py` on port 8765 |

Load/verify:

```sh
launchctl load ~/Library/LaunchAgents/com.leon.briefing.daily.plist
launchctl list | grep -E "briefing|yt-archive|process-ingest|transcript-server"
```

Logs:

```sh
tail -f /tmp/yt-archive.log        # archive + scrape pipeline
tail -f /tmp/yt-archive.err
tail -f ~/.claude/logs/launchd-briefing.out.log
tail -f ~/.claude/logs/launchd-process-ingest.out.log
```

Manual verification:

```sh
python3 ingest/briefing_sync.py              # today
python3 ingest/briefing_sync.py --date 2026-06-04  # backfill a specific date
python3 cli/daily_briefing_catchup.py        # only runs if today's note lacks Morning Briefing
python3 cli/daily_briefing_catchup.py --date 2026-06-04 --force
```

Use `cli/daily_briefing_catchup.py` when a Daily Note exists but rollover or
`## Morning Briefing ☀️` is missing. The catch-up command normalizes the note's
frontmatter/`Days:` preamble, preserves content already added by other workflows
such as Vault Housekeep, and then delegates briefing generation to
`ingest/briefing_sync.py`. If it writes `#degraded-sync`, credentials or a
downstream API failed after launch; rerun the catch-up command after fixing that
dependency. If the note still lacks `## Morning Briefing ☀️` and no catch-up log
exists, debug launchd scheduling first rather than assuming Google OAuth failed.

If run fails with:

```text
[ERROR] Google OAuth refresh token expired or revoked. Re-authorize by running: python3 cli/google_reauth.py
```

refresh token is dead. Re-auth:

```sh
python3 cli/google_reauth.py
```

## Hedy AI -> Obsidian vault

Pull today's Hedy sessions (meeting recaps, action items, highlights) into daily notes:

```sh
python3 ingest/hedy_sync.py
```

Pull sessions for a specific past date (e.g. backfill missed days):

```sh
python3 ingest/hedy_sync.py --date 2026-05-28
```

Pull sessions for an inclusive date range:

```sh
python3 ingest/hedy_sync.py --date 2026-05-28 --to 2026-06-12
```

That command:

- Authenticates against `https://api.hedy.bot/sessions` using `HEDY_AI_API_KEY` from `.env`
- Fetches the 10 most recent sessions for single-date syncs, or 100 sessions for range syncs
- Writes formatted recaps + to-dos into `z.Ingestion/Hedy-AI/YYYY-MM-DD.md`
- Extracts transcripts into `z.Ingestion/Hedy-AI/transcript YYYY-MM-DD.md`
- Auto-links keywords to vault pages via `KEYWORD_MAP`
- Skips sessions already present (idempotent)

Scheduling is handled by a LaunchAgent at `~/Library/LaunchAgents/com.leon.hedy-sync.plist` (daily at 5:30 AM).

## Transcript.lol -> Obsidian vault

Export all completed Transcript.lol recordings into the vault:

```sh
python3 cli/export_transcripts.py
```

That command:

- Authenticates against Transcript.lol using `.env`
- Lists recordings in `TRANSCRIPT_LOL_SPACE_ID`
- Exports completed recordings into `z.Ingestion/*.md`
- Skips recordings already exported
- Appends a `[[z.Ingestion/<title>]]` link into `Daily Notes/YYYY-MM-DD.md`

New YouTube ingestion notes are named `YYYYMMDD Title.md`. When the note uses
YouTube captions or a degraded transcript path, the fallback marker follows the
date: `YYYYMMDD *Title.md`. Existing notes are not renamed automatically.

Preview without writing files:
```sh
python3 cli/export_transcripts.py --dry-run
```

Write into a specific vault ingestion folder:

```sh
python3 cli/export_transcripts.py \
  --output-dir "~/Obsidian Vaults/AI-Vault/z.Ingestion"
```

Backfill transcript notes linked from daily notes in an inclusive date range. This re-runs the full `TranscriptService` pipeline for linked YouTube notes so missing or low-quality summaries can be overwritten with fresh Transcript.lol output:

```sh
python3 cli/export_transcripts.py \
  --from 2026-06-13 --to 2026-06-15 \
  --output-dir "~/Obsidian Vaults/AI-Vault/z.Ingestion"
```

Preview the backfill without writing files:

```sh
python3 cli/export_transcripts.py \
  --from 2026-06-13 --to 2026-06-15 \
  --output-dir "~/Obsidian Vaults/AI-Vault/z.Ingestion" \
  --dry-run
```

Backfill mode:

- Reads `Daily Notes/YYYY-MM-DD.md` for `[[z.Ingestion/*Title]]` links
- Re-opens matching `z.Ingestion/*.md` files and extracts source URL, title, and description
- Reprocesses only YouTube notes through `TranscriptService.save_from_url(..., mode="full")`
- Overwrites the existing note in place
- Skips missing daily notes, missing transcript notes, and non-YouTube links

If you want to submit one URL to Transcript.lol manually and print the transcript:

```sh
python3 cli/transcribe.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

## Important transcript examples

### 1. Print one transcript to stdout

YouTube still uses the existing Transcript.lol path:

```sh
python3 -u cli/transcribe.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Vimeo is captions-first. If Vimeo captions exist, the script prints them directly and does not create a Transcript.lol recording:

```sh
python3 -u cli/transcribe.py "https://vimeo.com/76979871"
```

If Vimeo captions do not exist, the script falls back to Transcript.lol. Failed imports now surface quickly because `_FAILED` statuses are treated as terminal failures:

```sh
python3 -u cli/transcribe.py \
  "https://player.vimeo.com/video/1187098771?app_id=122963"
```

### 2. Save one transcript into the vault***

Save a URL as markdown in `z.Ingestion/`:

```sh
python3 cli/transcript.py "https://vimeo.com/76979871"
```

Append links into an existing note at the same time:

```sh
python3 cli/transcript.py \
  --append-links-to-note "~/Obsidian Vaults/AI-Vault/AI Research Log.md" \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://vimeo.com/76979871"
```

### 3. Process iPhone-shared daily notes with bare YouTube URLs

If Obsidian creates a root-level `YYYY-MM-DD.md` containing bare YouTube URLs, `cli/scrape_notes.py` now routes each URL through `TranscriptService`, writes proper `*Title.md` files into `z.Ingestion/`, removes the URL lines from the date-ingest content, and skips the date-ingest file entirely when the note was only YouTube URLs.

Preview what will happen:

```sh
python3 cli/scrape_notes.py --dry-run
```

Run the ingest for any root-level `YYYY-MM-DD.md`, including older backfill dates:

```sh
python3 cli/scrape_notes.py
```

### 4. Reprocess old YouTube stub files

If older `z.Ingestion/*.md` files contain only bare YouTube URLs, reprocess them into normal titled transcript notes:

```sh
python3 cli/reprocess_youtube_stubs.py --dry-run
python3 cli/reprocess_youtube_stubs.py
```

The dry run should list each stub file and URL. The real run only deletes a stub after all URLs in that file succeed.

## Archive pipeline

Run the full YouTube archive chain manually:

```sh
bash cli/run_archive.sh
```

That runs in order:
1. `archive_youtube.py` — bare YouTube URLs from `Untitled*.md` / `New Note*.md` → `z.Ingestion/`
2. `daily_note_youtube.py` — bare YouTube URLs from `Daily Notes/YYYY-MM-DD.md` → `z.Ingestion/`
3. `scrape_notes.py` — bare YouTube URLs from root `YYYY-MM-DD.md` (iPhone share flow) → `z.Ingestion/`

The `com.ang.yt-archive` LaunchAgent runs this automatically whenever the vault root changes.

## Post-processing

After archival, OCR any embedded images and upload to Imgur:

```sh
python3 scripts/process_ingest.py --apply
```

Dry run:

```sh
python3 scripts/process_ingest.py
```

The `com.leon.process-ingest` LaunchAgent runs this automatically every 30s.

## Cross-vault transfer

## Weekly vault housekeeping

`ingest/weekly_housekeep_propose.py` is a PR-style, never-destructive vault cleaner. It scans the vault for empty notes, near-duplicates by first heading, and `Archive/` files older than 180 days, then writes a proposal report and a Daily Note addendum. The proposer never moves, renames, or deletes notes by itself.

Preview a week's proposals without writing anything:

```sh
python3 ingest/weekly_housekeep_propose.py --date 2026-07-04 --propose-only \
  --vault "/Users/leon/Obsidian Vaults/AI-Vault"
```

Write the proposal file plus the Saturday Daily Note addendum (idempotent — see below):

```sh
python3 ingest/weekly_housekeep_propose.py --date 2026-07-04
```

Apply approved proposals — refuses to run without an explicit human approval file:

```sh
# 1. Approve by writing a JSON file the script will read.
mkdir -p "$HOME/Hermes Output"
echo '{"proposal_id":"wk2026-27","ok":true}' > "$HOME/Hermes Output/housekeep.approve.json"
# 2. Run the wrapper (it refuses if the approval file or proposal is missing).
/Users/leon/.claude/scripts/apply-housekeep-proposals.sh
# 3. On success the approval file is cleared and the proposal is archived to
#    $HOME/.../AI-Vault/Hermes Output/applied/.
```

Try the negative path (no approval file) to confirm the wrapper refuses:

```sh
rm -f "$HOME/Hermes Output/housekeep.approve.json"
/Users/leon/.claude/scripts/apply-housekeep-proposals.sh
# -> [apply-housekeep] refusing: no approval file at ... housekeep.approve.json
# -> exit 1
```

Confidence scoring (used to pick which actions are autonomous vs. human review):

| Action kind | Confidence | Behaviour |
|---|---|---|
| `EMPTY_NOTE` | 0.97 | Routed to `MERGE` (non-archive) or `DEFER` (in `Archive/`). |
| `STALE_ARCHIVE` (≥365 days) | 0.97 | `DEFER`. |
| `STALE_ARCHIVE` (180–364 days) | 0.93 | `DEFER`. |
| `ORPHAN_TOPIC` (0 backlinks) | 0.90 | Routed to human review. |
| `NEAR_DUPLICATE` | 0.30–0.84 | Routed to human review (never autonomous). |

Schedule it with Hermes cron (created disabled-by-default so the first proposal can be reviewed by hand):

```sh
hermes cron list --profile lean
# to enable after review:
# hermes cron edit weekly-vault-housekeep   # set enabled=true and save
hermes cron run weekly-vault-housekeep      # one-off manual kickoff
```

Run the focused unit tests:

```sh
python3 -m pytest tests/test_weekly_housekeep_propose.py -q
```

Notes:
- The Daily Note addendum uses `<!-- housekeep-id:wkYYYY-Www -->` and `<!-- housekeep-until:YYYY-MM-DD -->` HTML comments so the same week rolls forward in the next 7 Morning Briefings.
- The proposer avoids `Daily Notes/` and `.dot*` paths, so it never audits notes owned by `briefing_sync.py` or the launchd workers.


Copy notes matching keywords to a second vault:

```sh
python3 cli/transfer_learning_to_neural.py --keywords "python" "AI" "LLM"
```

## Re-authorize Google OAuth2

If Google credentials expire or `briefing_sync.py` prints refresh-token error:

```sh
python3 cli/google_reauth.py
```

Opens a browser, captures the OAuth2 redirect on localhost, and writes updated tokens to `~/.config/vault-orchestrator/google_credentials`.

## Obsidian vault path

Default vault root used by transcript export:

`~/Obsidian Vaults/AI-Vault/` (override with `AI_VAULT_PATH`)
