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
| `ingest/mymemo_sync.py` | MyMemo AI API | Appends podcast digest to `Daily Notes/YYYY-MM-DD.md` |
| `ingest/briefing_sync.py` | Google Calendar + Gmail → MiniMax AI | Creates `Daily Notes/YYYY-MM-DD Briefing.md` |

## Setup

### 1. MyMemo credentials

```sh
mkdir -p ~/.config/mymemo
cat > ~/.config/mymemo/credentials << 'EOF'
{"m_authorization": "<JWT>", "auth0_cookie": "<cookie>"}
EOF
chmod 600 ~/.config/mymemo/credentials
```

Capture `m_authorization` from DevTools → Network tab → any `/api/*` request → Headers.

### 2. Google + MiniMax credentials

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

### 3. Crontab

```sh
crontab -e
```

Add:
```
# Daily briefing — 6 AM
0 6 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/briefing_sync.py >> /Users/leon/Library/Logs/briefing_sync.log 2>&1

# MyMemo digest — 11:50 PM
50 23 * * * /usr/bin/python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/mymemo_sync.py >> /Users/leon/Library/Logs/mymemo_sync.log 2>&1
```

## Running manually

```sh
python3 ingest/mymemo_sync.py
python3 ingest/briefing_sync.py
```

## Obsidian vault path

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Learning Root/`
