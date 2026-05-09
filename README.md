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
| `ingest/briefing_sync.py` | Google Calendar + Gmail → MiniMax AI | Creates or updates `Daily Notes/YYYY-MM-DD.md` |

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

### 2. Scheduling

Use the Claude wrapper plus a user LaunchAgent. The old direct cron entry below is
kept only as historical context because macOS Full Disk Access restrictions can
break `python3` when launched from cron.

```sh
/bin/zsh ~/.claude/scripts/run-briefing.sh
```

Install/load the LaunchAgent:
```
launchctl load ~/Library/LaunchAgents/com.leon.briefing.daily.plist
launchctl list | grep briefing
```

Manual verification:
```
python3 ingest/briefing_sync.py
```

## Obsidian vault path

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/AI-Vault/`
