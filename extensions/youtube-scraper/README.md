# YouTube Scraper Extension

## Load Unpacked

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select this folder:
   `/Users/leon/Documents/Code/vault-orchestrator/extensions/youtube-scraper`

## Pack Extension

1. Open `chrome://extensions`.
2. Click **Pack extension**.
3. Extension root directory:
   `/Users/leon/Documents/Code/vault-orchestrator/extensions/youtube-scraper`
4. Leave private key blank for first build.

## Usage

1. Open a YouTube watch page or Shorts page.
2. Click the extension icon.
3. Click **Scrape Current Video**.
4. Copy the JSON payload (title, url, language, summary, transcript).

## Notes

The popup now injects the scraper when you click the button. You do not need to refresh an already-open YouTube tab after loading or reloading the extension.
