import argparse
import json
from pathlib import Path

import requests

parser = argparse.ArgumentParser(description='')
parser.add_argument('-m', '--message', required=True,help="Message is required")
parser.add_argument('-f', '--file', default="", help="Optional file path to send as document")

args = parser.parse_args()
token = "8009331500:AAEy828Zmb1canIWjgjqAJEp3Q3DXHjyKuU"
chat_id = "-1002887118768"
thread_id = "5563"

# message = """
# @Meton_exir Ari to server barat payam gozashtam in 
# vpni ke dadi kar nemikone manam natoonestam vasl sham age in pmo khoondi too server 
# ba ./chat.sh -n Arian \"Message\" Javabamo bede (Nemitoonam biam telegram)
# """

file_path = Path(args.file).expanduser() if args.file else None
if file_path and file_path.is_file():
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "caption": args.message,
    }
    with file_path.open("rb") as handle:
        response = requests.post(url, data=data, files={"document": handle}, timeout=120)
else:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
            "chat_id": chat_id,
            "message_thread_id":thread_id,
            "text": args.message
        })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.post(url, headers=headers, data=payload, timeout=30)

print(response.json())

