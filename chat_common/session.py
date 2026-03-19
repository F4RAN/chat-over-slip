"""Shared session state and helper utilities for desktop frontends."""

from __future__ import annotations

import json
import importlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .transport import STATUS_POLL_INTERVAL

DEFAULT_DOMAIN = "t.qtn.at"
DEFAULT_REMOTE_SCRIPT = "~/chat-over-dnstt/chat.sh"
SLIPSTREAM_START_DELAY = 0.5
ONLINE_WINDOW_SECONDS = 300

KEYCHAIN_SERVICE = "chat-over-dnstt"


def parse_dns_result_file(path: str) -> List[Tuple[str, str]]:
    """Parse DNS IPs from a result.txt file."""
    resolved = Path(path).expanduser()
    entries: List[Tuple[str, str]] = []
    if not resolved.exists():
        return entries
    seen = set()
    for line in resolved.read_text().splitlines():
        if "IP:" not in line or "Time:" not in line:
            continue
        parts = line.split("IP:")[1].strip().split("-")
        ip = parts[0].strip()
        stamp = parts[1].replace("Time:", "").strip() if len(parts) > 1 else ""
        if ip and ip not in seen:
            seen.add(ip)
            entries.append((ip, stamp))
    return entries


def load_launcher_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


def save_launcher_state(state_path: Path, result: dict) -> None:
    state = load_launcher_state(state_path)
    if "host" in result:
        state["ssh"] = {
            "host": result.get("host", ""),
            "user": result.get("user", ""),
            "name": result.get("name", ""),
            "remote_script": result.get("remote_script", DEFAULT_REMOTE_SCRIPT),
            "remember_password": bool(result.get("remember_password", False)),
        }
    elif "ips" in result:
        state["dns"] = {
            "slip_path": str(result.get("slip_path", "")),
            "domain": result.get("domain", DEFAULT_DOMAIN),
            "user": result.get("user", ""),
            "name": result.get("name", ""),
            "remote_script": result.get("remote_script", DEFAULT_REMOTE_SCRIPT),
            "dns_file_path": result.get("dns_file_path", ""),
            "scanner_input_file": result.get("scanner_input_file", ""),
            "dns_extra": result.get("dns_extra", ""),
            "remember_password": bool(result.get("remember_password", False)),
        }
    state_path.write_text(json.dumps(state, indent=2))


def _run_security_command(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["security"] + args,
        capture_output=True,
        text=True,
        check=False,
    )


def _get_keyring():
    try:
        return importlib.import_module("keyring")
    except Exception:
        return None


def load_secure_password(secret_key: str) -> str:
    """Load password from OS keychain. Returns empty string if missing."""
    if not secret_key:
        return ""
    keyring_mod = _get_keyring()
    if keyring_mod is not None:
        try:
            value = keyring_mod.get_password(KEYCHAIN_SERVICE, secret_key)
            if value:
                return value
        except Exception:
            pass
    if sys.platform == "darwin" and shutil.which("security"):
        proc = _run_security_command(
            ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", secret_key, "-w"]
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    env_key = f"CHAT_DNSTT_PASSWORD_{secret_key.upper().replace('-', '_').replace(':', '_')}"
    return os.environ.get(env_key, "")


def save_secure_password(secret_key: str, password: str) -> None:
    """Save password in OS keychain and never in state JSON."""
    if not secret_key or password is None:
        return
    keyring_mod = _get_keyring()
    if keyring_mod is not None:
        try:
            keyring_mod.set_password(KEYCHAIN_SERVICE, secret_key, password)
            return
        except Exception:
            pass
    if sys.platform == "darwin" and shutil.which("security"):
        _run_security_command(
            [
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                secret_key,
                "-w",
                password,
            ]
        )
        return


def delete_secure_password(secret_key: str) -> None:
    """Delete saved password from secure store."""
    if not secret_key:
        return
    keyring_mod = _get_keyring()
    if keyring_mod is not None:
        try:
            keyring_mod.delete_password(KEYCHAIN_SERVICE, secret_key)
            return
        except Exception:
            pass
    if sys.platform == "darwin" and shutil.which("security"):
        _run_security_command(
            ["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", secret_key]
        )


def has_rtl(text: str) -> bool:
    for char in text or "":
        if "\u0590" <= char <= "\u08FF" or "\uFB50" <= char <= "\uFDFF" or "\uFE70" <= char <= "\uFEFF":
            return True
    return False


def rtl_wrap(text: str) -> str:
    if not text or not has_rtl(text):
        return text
    return "\u2067" + text + "\u2069"


def parse_file_message(text: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    import re

    file_match = re.match(r"^\[file\]\s+(.+?)::(.+)$", text)
    if file_match:
        name, relative_path = file_match.groups()
        return f"[file] {name}", (name, relative_path)

    tg_match = re.match(r"^\[TG file\]\s+(.+?)\s+saved to\s+(.+)$", text)
    if tg_match:
        name, saved_name = tg_match.groups()
        return f"[file] {name}", (name, f"tg_files/{saved_name}")

    return text, None


def resolve_download_target(available_files: Dict[str, str], query: str) -> Optional[Tuple[str, str]]:
    value = query.strip()
    if not value:
        return None
    if value in available_files:
        return value, available_files[value]
    for name, relative_path in available_files.items():
        if relative_path == value or relative_path.endswith(value):
            return name, relative_path
    return None


def extract_local_file_path(text: str) -> Optional[str]:
    line = text.strip()
    if not line:
        return None

    if line.startswith("file://"):
        parsed = urlparse(line)
        candidate = unquote(parsed.path)
        if candidate and Path(candidate).expanduser().is_file():
            return candidate

    try:
        parts = shlex.split(line)
    except ValueError:
        parts = [line]

    if len(parts) != 1:
        return None

    candidate = str(Path(parts[0]).expanduser())
    if Path(candidate).is_file():
        return candidate
    return None


def as_upload_command(text: str) -> Optional[str]:
    path = extract_local_file_path(text)
    if not path:
        return None
    return f"/upload {path}"


class NotificationPlayer:
    """Cross-platform best-effort notification sound playback."""

    @staticmethod
    def play(sound_path: Optional[str] = None) -> None:
        def _run() -> None:
            try:
                resolved_path = Path(sound_path).expanduser() if sound_path else None
                custom_sound = str(resolved_path) if resolved_path and resolved_path.exists() else None
                if sys.platform == "darwin":
                    subprocess.run(
                        ["afplay", custom_sound or "/System/Library/Sounds/Glass.aiff"],
                        timeout=5,
                        capture_output=True,
                        check=False,
                    )
                elif shutil.which("paplay"):
                    subprocess.run(
                        ["paplay", custom_sound or "/usr/share/sounds/freedesktop/stereo/message.oga"],
                        timeout=5,
                        capture_output=True,
                        check=False,
                    )
                elif shutil.which("aplay"):
                    if custom_sound:
                        cmd = ["aplay", "-q", custom_sound]
                    else:
                        cmd = ["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"]
                    subprocess.run(
                        cmd,
                        timeout=5,
                        capture_output=True,
                        check=False,
                    )
                else:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()


@dataclass
class MessageEntry:
    timestamp: str
    msg_id: str
    user: str
    raw_text: str
    display_text: str
    file_entry: Optional[Tuple[str, str]] = None
    pending: bool = False
    own: bool = False
    rtl: bool = False
    system: bool = False


@dataclass
class RenderResult:
    messages: List[MessageEntry]
    available_files: Dict[str, str]
    should_play_notification: bool


@dataclass
class ChatSessionModel:
    """UI-agnostic session state mirroring the old Textual behavior."""

    display_name: str
    last_snapshot: str = ""
    pending_messages: List[Tuple[str, str]] = field(default_factory=list)
    upload_status: Dict[str, str] = field(default_factory=dict)
    available_files: Dict[str, str] = field(default_factory=dict)
    seen_msg_ids: set = field(default_factory=set)
    user_last_seen: Dict[str, datetime] = field(default_factory=dict)

    def _parse_timestamp(self, timestamp: str) -> Optional[datetime]:
        try:
            return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def _message_entry(
        self,
        timestamp: str,
        msg_id: str,
        user: str,
        text: str,
        pending: bool = False,
        system: bool = False,
    ) -> MessageEntry:
        display_text, file_entry = parse_file_message(text)
        return MessageEntry(
            timestamp=timestamp,
            msg_id=msg_id,
            user=user,
            raw_text=text,
            display_text=display_text,
            file_entry=file_entry,
            pending=pending,
            own=user == self.display_name,
            rtl=has_rtl(text) or has_rtl(display_text),
            system=system,
        )

    def append_pending_message(self, text: str) -> List[MessageEntry]:
        self.pending_messages.append((self.display_name, text))
        return self.snapshot_messages(self.last_snapshot)

    def remove_pending_message(self, user: str, text: str) -> None:
        try:
            self.pending_messages.remove((user, text))
        except ValueError:
            pass

    def clear_messages(self) -> List[MessageEntry]:
        self.last_snapshot = ""
        self.available_files = {}
        self.seen_msg_ids.clear()
        return []

    def snapshot_messages(self, snapshot: str) -> List[MessageEntry]:
        result = self.render_snapshot(snapshot)
        return result.messages

    def render_snapshot(self, snapshot: str) -> RenderResult:
        messages: List[MessageEntry] = []
        available_files: Dict[str, str] = {}
        had_previous = len(self.seen_msg_ids) > 0
        should_play = False

        for line in snapshot.splitlines():
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    ts, msg_id, user, text = parts
                    if msg_id not in self.seen_msg_ids and had_previous and user != self.display_name:
                        should_play = True
                    self.seen_msg_ids.add(msg_id)
                    entry = self._message_entry(ts, msg_id, user, text)
                    parsed_ts = self._parse_timestamp(ts)
                    if parsed_ts and user and not user.startswith("news/"):
                        self.user_last_seen[user] = parsed_ts
                    if entry.file_entry:
                        name, relative_path = entry.file_entry
                        available_files[name] = relative_path
                    messages.append(entry)
                    continue
            messages.append(
                MessageEntry(
                    timestamp="",
                    msg_id="",
                    user="",
                    raw_text=line,
                    display_text=line,
                    system=True,
                )
            )

        for user, text in self.pending_messages:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            messages.append(self._message_entry(now, "", user, text, pending=True))

        self.last_snapshot = snapshot
        self.available_files = available_files
        return RenderResult(messages, available_files, should_play)

    def resolve_download(self, query: str) -> Optional[Tuple[str, str]]:
        return resolve_download_target(self.available_files, query)

    def online_users(self, now: Optional[datetime] = None) -> List[str]:
        current = now or datetime.now()
        active: List[str] = []
        for user, last_seen in self.user_last_seen.items():
            if (current - last_seen).total_seconds() <= ONLINE_WINDOW_SECONDS:
                active.append(user)
        active.sort(key=lambda item: (item != self.display_name, item.lower()))
        return active

    def should_retry_pending(self, mode: str, snapshot: Optional[str]) -> bool:
        return bool(snapshot is not None and self.pending_messages and mode == "ssh")

    def update_upload_status(self, label: str, percent: Optional[int], stage: str) -> str:
        if stage == "done":
            self.upload_status[label] = "100%"
        elif stage == "selected":
            self.upload_status[label] = "selected"
        elif stage == "failed":
            self.upload_status[label] = "failed"
        elif stage == "preparing":
            self.upload_status[label] = "preparing"
        elif stage == "probing":
            self.upload_status[label] = "probing"
        elif percent is not None:
            self.upload_status[label] = f"{percent}%"
        else:
            self.upload_status[label] = stage
        return self.transfer_status_text()

    def clear_upload_status(self) -> str:
        self.upload_status.clear()
        return ""

    def transfer_status_text(self) -> str:
        if not self.upload_status:
            return ""
        percent_items = [
            (label, status)
            for label, status in self.upload_status.items()
            if status.endswith("%")
        ]
        if percent_items:
            label, status = sorted(
                percent_items,
                key=lambda item: int(item[1][:-1]),
                reverse=True,
            )[0]
            return f"Transfer: {label} {status}"
        selected_items = [
            (label, status)
            for label, status in self.upload_status.items()
            if status == "selected"
        ]
        if selected_items:
            label, _status = selected_items[0]
            return f"Transfer: {label} selected"
        parts = [f"{label}: {status}" for label, status in sorted(self.upload_status.items())]
        return "Transfer: " + " | ".join(parts[:3])


class SlipstreamManager:
    """Start and stop local slipstream-client processes for DNSTT mode."""

    def __init__(self, slip_path: str, domain: str, dns_ips: List[str], proxy_ports: List[int]):
        self.slip_path = Path(slip_path).expanduser()
        self.domain = domain
        self.dns_ips = dns_ips
        self.proxy_ports = proxy_ports
        self.processes: List[subprocess.Popen] = []

    def start(self, on_status=None) -> None:
        for ip, port in zip(self.dns_ips, self.proxy_ports):
            if on_status:
                on_status(f"Starting DNS link: {ip}")
            proc = subprocess.Popen(
                [
                    "/usr/local/bin/slipstream-client",
                    "--tcp-listen-port",
                    str(port),
                    "--resolver",
                    f"{ip}:53",
                    "--domain",
                    self.domain,
                ],
                cwd=str(self.slip_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes.append(proc)
            time.sleep(SLIPSTREAM_START_DELAY)
        if on_status:
            on_status("Slipstream clients started. Waiting for links...")

    def stop(self) -> None:
        for proc in self.processes:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.processes = []

