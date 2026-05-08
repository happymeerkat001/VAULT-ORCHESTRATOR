import os
import re
import time
import base64
import requests
from dotenv import load_dotenv
from anthropic import Anthropic

# Load environment from the root .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Config
MINIMAX_KEY = os.getenv("MINIMAX_API_KEY")
BASE_URL = "https://api.minimax.io/anthropic"
VAULT_ROOT = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-orchestrator")

# Initialize Client
client = Anthropic(api_key=MINIMAX_KEY, base_url=BASE_URL)

def _read_icloud_file(path):
    """Read file, retrying on iCloud file-provider locks (EDEADLK under launchd)."""
    last_exc = None
    for _ in range(5):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            last_exc = exc
            time.sleep(1)
    raise last_exc


def _process_note(note_path):
    print(f"Checking {os.path.basename(note_path)} for images...")
    content = _read_icloud_file(note_path)

    # Matches ![image](https://i.imgur.com/xyz.jpg) but NOT ![Processed]
    pattern = r'!\[image\]\((https://i\.imgur\.com/[a-zA-Z0-9]+\.(?:jpg|jpeg|png))\)'
    matches = re.findall(pattern, content)

    if not matches:
        return

    for img_url in set(matches):
        print(f"Found Imgur link: {img_url}. Processing with MiniMax...")
        
        # Download
        img_data = base64.b64encode(requests.get(img_url).content).decode("utf-8")
        media_type = "image/png" if "png" in img_url else "image/jpeg"

        try:
            response = client.messages.create(
                model="MiniMax-M2.7",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                        {"type": "text", "text": "Transcribe this image. Use Markdown. If it's a mindmap, use hierarchy. If it's a document, be precise."}
                    ]
                }]
            )
            
            transcription = response.content[0].text
            
            # Replace ![image] with ![Processed] and append the text
            new_block = f"![Processed]({img_url})\n\n> [!abstract] AI Vision Transcription\n> {transcription.replace('\\n', '\\n> ')}\n"
            content = content.replace(f"![image]({img_url})", new_block)
            
        except Exception as e:
            print(f"Error processing {img_url}: {e}")

    last_exc = None
    for _ in range(10):
        try:
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(content)
            break
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5)
    else:
        raise last_exc or OSError(f"Unable to write {note_path}")

def process_active_notes():
    # This script will scan your last 3 daily notes for Imgur links
    # This ensures it catches anything you shared yesterday if the cron didn't run
    from datetime import datetime, timedelta
    
    for i in range(3):
        date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        candidate_dirs = [
            os.path.join(VAULT_ROOT, "Daily Notes"),
            VAULT_ROOT,
        ]
        for note_dir in candidate_dirs:
            note_path = os.path.join(note_dir, f"{date_str}.md")
            if os.path.exists(note_path):
                try:
                    _process_note(note_path)
                except OSError as e:
                    print(f"Skipping {note_path} (iCloud locked): {e}")

if __name__ == "__main__":
    process_active_notes()
