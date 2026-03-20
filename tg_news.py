#!/usr/bin/env python3
"""Fetch Telegram channel messages for the /news command."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Tuple

from telethon.sync import TelegramClient

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chat_common.env_file import load_repo_env

load_repo_env(_ROOT)

BASE_DIR = Path(__file__).resolve().parent
SESSION_PATH = BASE_DIR / "session"
NEWS_DIR = BASE_DIR / "tg_news"


def sanitize_field(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\t", " ")).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return slug.strip("-") or "channel"


def parse_range(spec: str) -> Tuple[int, int]:
    value = (spec or "10").strip()
    if "-" in value:
        start_text, end_text = value.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        if start <= end or end < 0:
            raise ValueError("range must be START-END, for example 20-10")
        return start - end, end

    limit = int(value)
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    return limit, 0


def emit_record(msg_id: str, name: str, text: str) -> None:
    safe_id = sanitize_field(msg_id)
    safe_name = sanitize_field(name)
    safe_text = sanitize_field(text)
    if safe_id and safe_name and safe_text:
        print(f"{safe_id}\t{safe_name}\t{safe_text}")


def download_media(client: TelegramClient, message, channel_slug: str) -> Tuple[str, str] | Tuple[None, None]:
    NEWS_DIR.mkdir(exist_ok=True)
    target_prefix = NEWS_DIR / f"{channel_slug}-{message.id}"
    saved_path = client.download_media(message, file=str(target_prefix))
    if not saved_path:
        return None, None

    saved = Path(saved_path)
    display_name = sanitize_field(getattr(getattr(message, "file", None), "name", "") or saved.name)
    return display_name, f"tg_news/{saved.name}"


def iter_records(
    channel: str, spec: str, api_id: int, api_hash: str
) -> Iterable[Tuple[str, str, str]]:
    limit, offset = parse_range(spec)
    channel_slug = slugify(channel)
    sender_name = f"news/{channel_slug}"

    with TelegramClient(str(SESSION_PATH), api_id, api_hash) as client:
        messages = client.get_messages(channel, limit=limit, add_offset=offset)
        for message in reversed(list(messages)):
            base_id = f"news-{channel_slug}-{message.id}"
            text = sanitize_field(message.message or "")
            if text:
                yield base_id, sender_name, text

            if getattr(message, "media", None):
                display_name, relative_path = download_media(client, message, channel_slug)
                if display_name and relative_path:
                    yield (
                        f"{base_id}-file",
                        sender_name,
                        f"[file] {display_name}::{relative_path}",
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Telegram channel messages for chat news import.")
    parser.add_argument("channel", help="Telegram channel username or link")
    parser.add_argument("range_spec", nargs="?", default="10", help="Count or START-END range, e.g. 10 or 20-10")
    args = parser.parse_args()

    raw_id = os.environ.get("TG_NEWS_API_ID", "").strip()
    api_hash = os.environ.get("TG_NEWS_API_HASH", "").strip()
    if not raw_id or not api_hash:
        print(
            "tg_news error: set TG_NEWS_API_ID and TG_NEWS_API_HASH (see .env.example)",
            file=sys.stderr,
        )
        return 1
    try:
        api_id = int(raw_id)
    except ValueError:
        print("tg_news error: TG_NEWS_API_ID must be an integer", file=sys.stderr)
        return 1

    try:
        for msg_id, name, text in iter_records(args.channel, args.range_spec, api_id, api_hash):
            emit_record(msg_id, name, text)
    except Exception as exc:
        print(f"tg_news error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())