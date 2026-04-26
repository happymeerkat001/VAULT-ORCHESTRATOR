# vault-orchestrator

A strict read-only orchestration pipeline that pulls data from external services, processes it locally, and delivers formatted markdown into an Obsidian vault via iCloud.

## Design principles

- **Read-only.** Never mutates external services (no email sends, no calendar edits, no deletes).
- **Zero dependencies.** Pure Python 3 stdlib — no pip installs, no virtual environments.
- **Token-efficient.** Data is filtered and formatted locally before any LLM call.
- **Composable.** Each stage (ingest → process → deliver) is decoupled and independently runnable.

## Folder structure

```
ingest/     One file per external data source. Fetches raw API data.
process/    Pure Python filtering, parsing, and formatting. No network calls.
deliver/    Writes final markdown to the Obsidian vault path.
cli/        Manual trigger scripts for one-shot runs.
```

## Data sources

| Script | Source | Destination |
|--------|--------|-------------|
| `ingest/youtube_sync.py` | YouTube watch + captions endpoints | Appends transcript + summary to `Daily Notes/YYYY-MM-DD.md` |
| `ingest/briefing_sync.py` | Google Calendar + Gmail → MiniMax AI | Creates `Daily Notes/YYYY-MM-DD Briefing.md` |

## Setup

### 1. Google + MiniMax credentials

```sh
mkdir -p ~/.config/vault-orchestrator
cat > ~/.config/vault-orchestrator/google_credentials << 'EOF'
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

### 2. Crontab

```sh
crontab -e
```

Add:
```
# Daily briefing — 6 AM
0 6 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/briefing_sync.py >> /Users/leon/Library/Logs/briefing_sync.log 2>&1

# YouTube transcript pull — 11:50 PM (replace URL)
50 23 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/youtube_sync.py --url "https://www.youtube.com/watch?v=VIDEO_ID" >> /Users/leon/Library/Logs/youtube_sync.log 2>&1
```

## Running manually

```sh
python3 ingest/youtube_sync.py --url "https://www.youtube.com/watch?v=VIDEO_ID"
python3 ingest/briefing_sync.py
```

## Obsidian vault path

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator/`
