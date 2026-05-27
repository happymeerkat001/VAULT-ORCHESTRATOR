Process and organize the Obsidian vault ingestion pipeline.

```bash
python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/scripts/process_ingest.py --apply --verbose
nohup python3 -u /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/scrape_notes.py >> /tmp/scrape-notes-yt.log 2>&1 &
echo "[obsidian] YouTube root notes queued (PID $!). Log: /tmp/scrape-notes-yt.log"
nohup python3 -u /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/reprocess_youtube_stubs.py >> /tmp/reprocess-yt.log 2>&1 &
echo "[obsidian] YouTube z.Ingestion stubs queued (PID $!). Log: /tmp/reprocess-yt.log"
```

Print stdout/stderr exactly as returned. Do not reformat or summarize.
If the script exits non-zero, show the error. Suggest running with `--dry-run` first if the user wants to preview changes.
