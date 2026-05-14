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
| `cli/export_transcripts.py` | Transcript.lol recordings | Writes notes into `z.Ingestion/` and appends links into today's daily note |
| `cli/transcribe.py` | Single URL via Transcript.lol | Prints transcript text to stdout |

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

## Obsidian vault path

Default vault root used by transcript export:

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/`
