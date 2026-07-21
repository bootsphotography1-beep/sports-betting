"""Quick Telegram delivery test. Reads token from .env, sends a test message."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6179209408").strip()

if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
    sys.exit(2)

msg = (
    "STALE-ODDS CONFIDENCE: ~85%\n"
    "Telegram bot is LIVE!\n"
    f"Bot: @Sportanalyst123457_bot\n"
    f"Chat ID: {CHAT_ID}\n"
    f"Test time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
    "You should see this message in Telegram."
)

r = requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={"chat_id": CHAT_ID, "text": msg},
    timeout=15,
)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")
sys.exit(0 if r.ok else 1)
