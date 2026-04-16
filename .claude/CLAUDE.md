# vault-orchestrator

Control-orchestration pipeline that builds daily briefings in Obsidian.

## CRITICAL: READ-ONLY CONTRACT

This system NEVER performs destructive or mutating API calls on external services.
Allowed: GET, list, read, fetch.
Forbidden: delete, update, patch, send, modify — on any external API (Gmail, Calendar, etc.).

## Architecture

```
ingest/     Raw API fetchers. One file per data source. Returns structured dicts.
process/    Local formatting, filtering, and parsing. No LLM calls here.
deliver/    Writes final markdown to Obsidian vault via iCloud path.
cli/        One-shot trigger scripts for manual runs.
```

## Token minimization rule

LLM calls (MiniMax, Claude) are expensive. Minimize their input:
- Pre-filter in `/process` using Python regex/parsing before passing to any LLM.
- Strip boilerplate, quoted text, and metadata noise before building prompts.
- Never pass raw API responses directly to an LLM. Always process first.

## Dependencies

Zero pip packages. Stdlib only (`urllib`, `json`, `pathlib`, `re`, `datetime`).

## Credentials

All secrets live in `~/.config/` as JSON files, chmod 600, never committed.
- `~/.config/mymemo/credentials` — MyMemo JWT + auth0 cookie
- `~/.config/vault-orchestrator/google_credentials` — Google OAuth2 + MiniMax API key

## Obsidian vault root

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator/`

## Scripts

| File | Data source | Output |
|------|-------------|--------|
| `ingest/mymemo_sync.py` | MyMemo AI API | Appends podcast digests to `YYYY-MM-DD.md` |
| `ingest/briefing_sync.py` | Google Calendar + Gmail → MiniMax | Creates `YYYY-MM-DD Briefing.md` |
| `ingest/hedy_sync.py` | Hedy AI sessions API | Appends session summaries + action items to `YYYY-MM-DD.md` |
