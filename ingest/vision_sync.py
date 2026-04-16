#!/usr/bin/env python3
"""
vision_sync.py — Transcribes scanned images in Obsidian vault via Claude Vision
and writes Markdown sidecar files next to each image.

One-time setup:
  1. mkdir -p ~/.config/anthropic
  2. cat > ~/.config/anthropic/credentials << 'JSON'
     {"api_key": "sk-ant-..."}
     JSON
  3. chmod 600 ~/.config/anthropic/credentials

Manual run:
  python3 /Users/leon/Documents/Code/vault-orchestrator/ingest/vision_sync.py

No external dependencies — stdlib only.
"""

import base64
import json
import mimetypes
import sys
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS_PATH = Path("~/.config/anthropic/credentials").expanduser()
VAULT_ROOT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-Orchestrator"
).expanduser()
SCANS_DIR = VAULT_ROOT / "Attachments/Scans"
MODEL = "claude-3-5-sonnet-20241022"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
PROMPT = (
    "Transcribe this diagram into a clean Markdown outline. "
    "Use [[Wikilinks]] for entities (people/companies). "
    "Maintain visual hierarchy."
)
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png"}


def load_api_key() -> str:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}\n"
            'Create it with JSON: {"api_key": "sk-ant-..."}'
        )

    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        creds = json.load(f)

    api_key = creds.get("api_key")
    if not api_key:
        raise ValueError(f"Missing 'api_key' in {CREDENTIALS_PATH}")
    return api_key


def detect_media_type(image_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(image_path))
    if guessed in {"image/jpeg", "image/png"}:
        return guessed
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "image/png"


def transcribe_image(image_path: Path, api_key: str) -> str:
    b64_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    media_type = detect_media_type(image_path)

    payload = {
        "model": MODEL,
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    }

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        response = json.loads(resp.read().decode("utf-8"))

    blocks = response.get("content", [])
    text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    transcript = "\n\n".join(part.strip() for part in text_parts if part.strip())
    if not transcript:
        raise RuntimeError(f"No transcript text returned for {image_path.name}")
    return transcript


def build_markdown(image_name: str, transcript: str) -> str:
    return f"![[{image_name}]]\n\n{transcript.rstrip()}\n"


def main() -> int:
    try:
        api_key = load_api_key()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not SCANS_DIR.exists():
        print(f"Scans directory not found: {SCANS_DIR}", file=sys.stderr)
        return 1

    image_paths = sorted(
        p for p in SCANS_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )

    if not image_paths:
        print(f"No scan images found in {SCANS_DIR}")
        return 0

    processed = 0
    skipped = 0

    for image_path in image_paths:
        md_path = image_path.with_suffix(".md")
        if md_path.exists():
            print(f"SKIP  {image_path.name} (sidecar exists)")
            skipped += 1
            continue

        print(f"TRANSCRIBE {image_path.name} ...")
        try:
            transcript = transcribe_image(image_path, api_key)
            md_path.write_text(build_markdown(image_path.name, transcript), encoding="utf-8")
            print(f"WROTE {md_path.name}")
            processed += 1
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"ERROR {image_path.name}: HTTP {exc.code} {body}", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"ERROR {image_path.name}: Network error: {exc.reason}", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR {image_path.name}: {exc}", file=sys.stderr)

    print(f"Done. processed={processed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
