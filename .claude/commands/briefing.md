Run the morning briefing workflow:

```bash
python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/ingest/briefing_sync.py
```

Print stdout/stderr exactly as returned. Do not reformat or summarize.
If the script exits non-zero, tell the user: "Briefing sync failed. Check the error above and verify credentials in `~/.config/vault-orchestrator/google_credentials`."
