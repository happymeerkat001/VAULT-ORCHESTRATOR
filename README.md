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
| `ingest/hedy_sync.py` | Hedy AI sessions API | Writes recaps + action items to `Hedy-AI/YYYY-MM-DD.md` |
| `cli/export_transcripts.py` | Transcript.lol recordings | Writes notes into `z.Ingestion/` and appends links into today's daily note |
| `cli/transcribe.py` | Single media URL | Prints transcript text to stdout |
| `cli/transcript.py` | One or more media URLs | Saves transcript markdown into `z.Ingestion/` |
| `cli/scrape_notes.py` | Root-level `YYYY-MM-DD.md` notes | Archives note text/OCR and routes bare YouTube URLs into transcript saves |

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

`cli/transcribe.py` and `cli/export_transcripts.py` read Transcript.lol auth from repo-root `.env`.

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

Quick auth check:

```sh
python3 cli/transcribe.py --test-auth
```

### 3. Scheduling

Use the Claude wrapper plus a user LaunchAgent. The old direct cron entry below is kept only as historical context because macOS Full Disk Access restrictions can break `python3` when launched from cron.

```sh
/bin/zsh ~/.claude/scripts/run-briefing.sh
```
Example:   python3 cli/transcribe.py
  "https://player.vimeo.com/video/1187098771?app_id=122963"
  
Install/load the LaunchAgent:

```sh
launchctl load ~/Library/LaunchAgents/com.leon.briefing.daily.plist
launchctl list | grep briefing
```

Manual verification:

```sh
python3 ingest/briefing_sync.py
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

That command:

- Authenticates against `https://api.hedy.bot/sessions` using `HEDY_AI_API_KEY` from `.env`
- Fetches the 10 most recent sessions, filters to today
- Writes formatted recaps + to-dos into `Hedy-AI/YYYY-MM-DD.md`
- Extracts transcripts into `Hedy-AI/transcript YYYY-MM-DD.md`
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

Preview without writing files:

```sh
python3 cli/export_transcripts.py --dry-run
```

Write into a specific vault ingestion folder:

```sh
python3 cli/export_transcripts.py \
  --output-dir "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/z.Ingestion"
```

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
  --append-links-to-note "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/AI Research Log.md" \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://vimeo.com/76979871"
```

### 3. Process iPhone-shared daily notes with bare YouTube URLs

If Obsidian creates a root-level `YYYY-MM-DD.md` containing bare YouTube URLs, `cli/scrape_notes.py` now routes each URL through `TranscriptService`, writes proper `*Title.md` files into `z.Ingestion/`, removes the URL lines from the date-ingest content, and skips the date-ingest file entirely when the note was only YouTube URLs.

Preview what will happen:

```sh
python3 cli/scrape_notes.py --dry-run
```

Run the ingest:

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

## Obsidian vault path

Default vault root used by transcript export:

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/`
