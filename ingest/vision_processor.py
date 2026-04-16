import os
import re
import base64
import requests
from dotenv import load_dotenv
from anthropic import Anthropic

# Load environment from the root .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Config
MINIMAX_KEY = os.getenv("MINIMAX_API_KEY")
BASE_URL = "https://api.minimax.io/anthropic"
VAULT_ROOT = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Neural-Orchestrator")

# Initialize Client
client = Anthropic(api_key=MINIMAX_KEY, base_url=BASE_URL)

def process_active_notes():
    # This script will scan your last 3 daily notes for Imgur links
    # This ensures it catches anything you shared yesterday if the cron didn't run
    from datetime import datetime, timedelta
    
    for i in range(3):
        date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        note_path = os.path.join(VAULT_ROOT, f"{date_str}.md")
        
        if not os.path.exists(note_path):
            continue
            
        print(f"Checking {date_str}.md for images...")
        with open(note_path, "r") as f:
            content = f.read()

        # Matches ![image](https://i.imgur.com/xyz.jpg) but NOT ![Processed]
        pattern = r'!\[image\]\((https://i\.imgur\.com/[a-zA-Z0-9]+\.(?:jpg|jpeg|png))\)'
        matches = re.findall(pattern, content)

        if not matches:
            continue

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

        with open(note_path, "w") as f:
            f.write(content)

if __name__ == "__main__":
    process_active_notes()