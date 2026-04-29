# YouTube -> Obsidian Transcript

## 1) Start local server

```bash
python3 /Users/leon/Documents/Code/vault-orchestrator/cli/transcript_server.py
```

Server listens on `http://localhost:8765`.

## 2) Load extension in Chrome

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder: `chrome-extension/`

## 3) Use on YouTube

1. Open any `youtube.com/watch` video page
2. Click **Save Transcript**
3. Status text shows success or error

If you see `Start transcript_server.py first`, the local server is not running.
