import argparse
import json
import os
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chat_common.env_file import load_repo_env

load_repo_env(_ROOT)

parser = argparse.ArgumentParser(description="")
parser.add_argument("-m", "--message", required=True, help="Message is required")
parser.add_argument("-f", "--file", default="", help="Optional file path to send as document")

args = parser.parse_args()

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
thread_id = os.environ.get("TELEGRAM_NOTIFY_MESSAGE_THREAD_ID", "").strip()

if not token or not chat_id:
    sys.exit(0)

file_path = Path(args.file).expanduser() if args.file else None
if file_path and file_path.is_file():
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": chat_id, "caption": args.message}
    if thread_id:
        data["message_thread_id"] = thread_id
    with file_path.open("rb") as handle:
        response = requests.post(url, data=data, files={"document": handle}, timeout=120)
else:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": args.message}
    if thread_id:
        payload["message_thread_id"] = thread_id
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=30,
    )

print(response.json())
