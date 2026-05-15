Process and organize the Obsidian vault ingestion pipeline.

```bash
python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/scripts/process_ingest.py --apply --verbose
python3 /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli/reprocess_youtube_stubs.py
```

Print stdout/stderr exactly as returned. Do not reformat or summarize.
If the script exits non-zero, show the error. Suggest running with `--dry-run` first if the user wants to preview changes.
