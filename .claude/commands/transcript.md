Save transcripts for media URLs into the Obsidian vault.

Ask the user for one or more URLs if not provided as arguments: $ARGUMENTS

```bash
cd /Users/leon/Documents/Code/Obsidian-vault-orchestrator && python3 cli/transcript.py "$ARGUMENTS"
```

Print stdout/stderr exactly as returned. Do not reformat or summarize.
If the script exits non-zero, show the error and suggest checking that `yt-dlp` is installed (`pip3 install yt-dlp`).
