#!/usr/bin/env python3
"""Poll Telegram bot updates and append them into chat.sh."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chat_common.env_file import load_repo_env

load_repo_env(_ROOT)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    print(
        "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and configure Telegram.",
        file=sys.stderr,
    )
    sys.exit(1)

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
BASE_DIR = Path(__file__).resolve().parent
OFFSET_FILE = BASE_DIR / ".tg_offset"
CHAT_SCRIPT = BASE_DIR / "chat.sh"
TG_FILES_DIR = BASE_DIR / "tg_files"


def load_offset():
    if OFFSET_FILE.exists():
        return int(OFFSET_FILE.read_text().strip() or "0")
    return 0


def save_offset(offset):
    OFFSET_FILE.write_text(str(offset))


def append_chat(name, msg_id, text):
    subprocess.run(
        ["bash", str(CHAT_SCRIPT), "-n", name, msg_id, text],
        cwd=str(BASE_DIR),
        check=False,
    )


def download_document(file_id, filename):
    TG_FILES_DIR.mkdir(exist_ok=True)
    resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        return None
    file_path = data["result"]["file_path"]
    content = requests.get(f"{FILE_API}/{file_path}", timeout=60)
    if content.status_code != 200:
        return None
    out = TG_FILES_DIR / filename
    out.write_bytes(content.content)
    return out


def handle_message(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    sender = message.get("from", {})
    if sender.get("is_bot"):
        return

    name = sender.get("username") or sender.get("first_name") or "telegram"
    msg_id = f"tg-{update['update_id']}"

    text = (message.get("text") or message.get("caption") or "").strip()
    if text:
        append_chat(f"tg/{name}", msg_id, text)

    document = message.get("document")
    if document:
        filename = document.get("file_name") or f"{document.get('file_unique_id', 'file')}.bin"
        saved = download_document(document["file_id"], filename)
        note = f"[file] {filename}"
        if saved:
            note += f"::tg_files/{saved.name}"
        append_chat(f"tg/{name}", f"{msg_id}-file", note)


def poll_once(offset):
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={"offset": offset + 1, "timeout": 25},
        timeout=35,
    )
    data = resp.json()
    if not data.get("ok"):
        return offset

    for update in data.get("result", []):
        offset = max(offset, update["update_id"])
        handle_message(update)
    return offset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    offset = load_offset()
    while True:
        try:
            offset = poll_once(offset)
            save_offset(offset)
            if args.once:
                break
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Telegram poll error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
