#!/bin/zsh
cd /Users/leon/Documents/Code/Obsidian-vault-orchestrator/cli
sleep 5
/usr/local/bin/python3 archive_youtube.py
/usr/local/bin/python3 daily_note_youtube.py
/usr/local/bin/python3 scrape_notes.py
