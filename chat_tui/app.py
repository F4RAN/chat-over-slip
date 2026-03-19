"""Simple chat TUI backed by SSH commands."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from rich import box
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual import events
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

# Tunable parameters for poor or unstable networks.
SSH_CONNECT_TIMEOUT = 45
SSH_SERVER_ALIVE_INTERVAL = 15
SSH_SERVER_ALIVE_COUNT_MAX = 2
REMOTE_COMMAND_TIMEOUT = 60
FILE_TRANSFER_TIMEOUT = 180
NEWS_COMMAND_TIMEOUT = 180
SCP_PROGRESS_POLL_INTERVAL = 0.25
STATUS_POLL_INTERVAL = 3
SOFT_ERROR_OK_GRACE_FAILURES = 3
SOFT_ERROR_UNKNOWN_GRACE_FAILURES = 2
SSH_SEND_RETRIES = 3
SSH_SEND_RETRY_DELAY = 2
DNS_LINK_MAX_RETRIES = 2
APP_RUNTIME_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
APP_RESOURCE_ROOT = (
    Path(getattr(sys, "_MEIPASS")).resolve()
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)


class ChatTransport:
    def __init__(
        self,
        mode: str,
        ssh_user: str,
        ssh_pass: str,
        remote_script: str,
        host: str = "",
        domain: str = "",
        proxy_ports: Optional[List[int]] = None,
        dns_ips: Optional[List[str]] = None,
    ):
        self.mode = mode
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self.remote_script = self._normalize_remote_script(remote_script)
        self.host = host
        self.domain = domain
        self.proxy_ports = proxy_ports or []
        self.dns_ips = dns_ips or []
        if mode == "dns":
            self.status = {ip: "unknown" for ip in self.dns_ips}
            self.fail_counts = {ip: 0 for ip in self.dns_ips}
            self.last_online_at = {ip: None for ip in self.dns_ips}
        else:
            self.status = {"ssh": "unknown"}
            self.fail_counts = {"ssh": 0}
            self.last_online_at = {"ssh": None}
        self.retry_counts: Dict[str, int] = {}
        self._pending_restarts: List[str] = []
        self.ready_ips: Set[str] = set()
        self.last_error = ""

    def _normalize_remote_script(self, path: str) -> str:
        path = (path or "~/chat-over-dnstt/chat.sh").strip()
        if path.startswith("~/"):
            return "${HOME}/" + path[2:]
        return path

    def _base_ssh_cmd(self, proxy_port: Optional[int] = None) -> List[str]:
        cmd = [
            "sshpass",
            "-p",
            self.ssh_pass,
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            "-o",
            f"ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL}",
            "-o",
            f"ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}",
        ]
        target = self.host if self.mode == "ssh" else self.domain
        if proxy_port is not None:
            cmd += ["-o", f"ProxyCommand nc 127.0.0.1 {proxy_port}"]
        cmd.append(f"{self.ssh_user}@{target}")
        return cmd

    def _remote_target(self) -> str:
        target = self.host if self.mode == "ssh" else self.domain
        return f"{self.ssh_user}@{target}"

    def _remote_base_dir(self) -> str:
        if self.remote_script.startswith("${HOME}/"):
            return "${HOME}/" + self.remote_script[len("${HOME}/") :].rsplit("/", 1)[0]
        return str(Path(self.remote_script).parent)

    def _remote_file_path(self, relative_path: str) -> str:
        return f"{self._remote_base_dir().rstrip('/')}/{relative_path.lstrip('/')}"

    def _base_scp_cmd(self, proxy_port: Optional[int] = None) -> List[str]:
        cmd = [
            "sshpass",
            "-p",
            self.ssh_pass,
            "scp",
            "-O",
            "-o",
            "BatchMode=no",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        ]
        if proxy_port is not None:
            cmd += ["-o", f"ProxyCommand nc 127.0.0.1 {proxy_port}"]
        return cmd

    def _remote_run(
        self,
        remote_command: str,
        proxy_port: Optional[int] = None,
        timeout: Optional[int] = REMOTE_COMMAND_TIMEOUT,
    ) -> subprocess.CompletedProcess:
        cmd = self._base_ssh_cmd(proxy_port) + [f"bash -lc {shlex.quote(remote_command)}"]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _run_read_once(self, proxy_port: Optional[int] = None, limit: int = 200) -> Tuple[str, str]:
        remote_command = f"bash {self.remote_script} -r {limit}"
        try:
            proc = self._remote_run(remote_command, proxy_port=proxy_port, timeout=REMOTE_COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "unknown", ""
        except Exception as exc:
            self.last_error = str(exc)
            return "fail", ""
        if proc.returncode != 0:
            self.last_error = proc.stderr.strip() or proc.stdout.strip() or "read failed"
            return "fail", proc.stdout
        return "ok", proc.stdout

    def _run_link_command(
        self,
        ip: str,
        port: int,
        remote_command: str,
        timeout: Optional[int] = REMOTE_COMMAND_TIMEOUT,
    ) -> Tuple[str, str, str, str]:
        try:
            proc = self._remote_run(remote_command, proxy_port=port, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ip, "unknown", "", "waiting for network"
        except Exception as exc:
            return ip, "fail", "", str(exc)
        if proc.returncode != 0:
            return ip, "fail", proc.stdout, proc.stderr.strip() or proc.stdout.strip() or "command failed"
        return ip, "ok", proc.stdout, ""

    def _soft_error(self, error: str) -> bool:
        text = (error or "").lower()
        soft_markers = [
            "timed out",
            "connection closed",
            "banner exchange",
            "unknown port 65535",
            "waiting for network",
            "kex_exchange_identification",
            "connection reset by peer",
            "connection reset",
            "read: connection reset",
        ]
        return any(marker in text for marker in soft_markers)

    def _run_scp_with_progress(
        self,
        scp_cmd: List[str],
        label: str,
        timeout: int,
        progress_cb: Optional[Callable[[str, Optional[int], str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[bool, str]:
        master_fd, slave_fd = pty.openpty()
        output = bytearray()
        last_percent: Optional[int] = None
        proc: Optional[subprocess.Popen] = None

        try:
            proc = subprocess.Popen(
                scp_cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            if progress_cb:
                progress_cb(label, 0, "starting")

            deadline = time.monotonic() + timeout
            while True:
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    return False, "cancelled"

                if proc.poll() is not None:
                    break

                if time.monotonic() > deadline:
                    proc.kill()
                    return False, "upload timed out"

                readable, _, _ = select.select([master_fd], [], [], SCP_PROGRESS_POLL_INTERVAL)
                if not readable:
                    continue

                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""

                if not chunk:
                    continue

                output.extend(chunk)
                decoded = output[-4096:].decode(errors="ignore")
                matches = re.findall(r"(\d+)%", decoded)
                if matches:
                    percent = int(matches[-1])
                    if percent != last_percent:
                        last_percent = percent
                        if progress_cb:
                            progress_cb(label, percent, "uploading")

            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)

            if proc.returncode == 0:
                if progress_cb:
                    progress_cb(label, 100, "done")
                return True, ""

            text = output.decode(errors="ignore").strip()
            return False, text or "upload failed"
        finally:
            if proc and proc.poll() is None:
                proc.kill()
            try:
                os.close(master_fd)
            except OSError:
                pass
            if slave_fd != -1:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

    def _needs_restart(self, error: str) -> bool:
        return "connection closed by unknown port 65535" in (error or "").lower()

    def _mark_success(self, label: str) -> None:
        self.status[label] = "ok"
        self.fail_counts[label] = 0
        self.retry_counts[label] = 0
        self.last_online_at[label] = datetime.now(timezone.utc)

    def _mark_failure(self, label: str, error: str) -> None:
        if self.retry_counts.get(label, 0) >= DNS_LINK_MAX_RETRIES and self._needs_restart(error):
            self.status[label] = "fail"
            return
        self.fail_counts[label] = self.fail_counts.get(label, 0) + 1
        if self._needs_restart(error):
            self.retry_counts[label] = self.retry_counts.get(label, 0) + 1
            self.status[label] = "unknown"
            self.fail_counts[label] = 0
            if label not in self._pending_restarts:
                self._pending_restarts.append(label)
        elif self._soft_error(error):
            if self.status.get(label) == "ok" and self.fail_counts[label] < SOFT_ERROR_OK_GRACE_FAILURES:
                self.status[label] = "ok"
            elif self.fail_counts[label] < SOFT_ERROR_UNKNOWN_GRACE_FAILURES:
                self.status[label] = "unknown"
            else:
                self.status[label] = "fail"
        else:
            self.status[label] = "fail"

    def pop_pending_restarts(self) -> List[str]:
        restarts = self._pending_restarts[:]
        self._pending_restarts.clear()
        return restarts

    def last_online_age_seconds(self, label: str, now: Optional[datetime] = None) -> Optional[int]:
        when = self.last_online_at.get(label)
        if when is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        return max(0, int((now - when).total_seconds()))

    def remove_dns_link(self, label: str) -> bool:
        if self.mode != "dns":
            return False
        if label not in self.dns_ips:
            return False
        idx = self.dns_ips.index(label)
        self.dns_ips.pop(idx)
        if idx < len(self.proxy_ports):
            self.proxy_ports.pop(idx)
        self.status.pop(label, None)
        self.fail_counts.pop(label, None)
        self.last_online_at.pop(label, None)
        return True

    @staticmethod
    def normalize_news_range(range_spec: str) -> str:
        value = (range_spec or "").strip()
        if not value:
            return "10"
        if " " in value and "-" not in value:
            parts = [p for p in value.split() if p]
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                return f"{parts[0]}-{parts[1]}"
        return value

    def read_messages(self, limit: int = 200) -> Tuple[Optional[str], Dict[str, str]]:
        if self.mode == "ssh":
            state, output = self._run_read_once(limit=limit)
            if state == "ok":
                self._mark_success("ssh")
            else:
                self._mark_failure("ssh", self.last_error)
            return (output if state == "ok" else None), dict(self.status)

        first_output = None
        remote_command = f"bash {self.remote_script} -r {limit}"
        active = [
            (ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports)
            if ip in self.ready_ips
        ]
        if not active:
            return None, dict(self.status)
        with ThreadPoolExecutor(max_workers=max(1, len(active))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
                for ip, port in active
            ]
            for future in as_completed(futures):
                ip, state, output, error = future.result()
                if state == "ok":
                    self._mark_success(ip)
                else:
                    self._mark_failure(ip, error)
                if error and state == "fail":
                    self.last_error = f"{ip}: {error}"
                elif state == "ok" and first_output is None:
                    first_output = output
        return first_output, dict(self.status)

    def send_message(self, name: str, text: str) -> Tuple[bool, str, Dict[str, str]]:
        msg_id = uuid.uuid4().hex
        remote_command = (
            f"bash {self.remote_script} -n "
            f"{shlex.quote(name)} {shlex.quote(msg_id)} {shlex.quote(text)}"
        )

        if self.mode == "ssh":
            last_exc = None
            last_proc = None
            for attempt in range(SSH_SEND_RETRIES):
                try:
                    last_proc = self._remote_run(
                        remote_command, timeout=REMOTE_COMMAND_TIMEOUT
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    self.last_error = str(exc)
                    if attempt < SSH_SEND_RETRIES - 1:
                        time.sleep(SSH_SEND_RETRY_DELAY)
                    else:
                        self._mark_failure("ssh", str(exc))
                        return False, str(exc), dict(self.status)
            proc = last_proc
            if proc.returncode == 0:
                self._mark_success("ssh")
            else:
                err = proc.stderr.strip() or proc.stdout.strip() or "send failed"
                self._mark_failure("ssh", err)
                self.last_error = err
            return proc.returncode == 0, (proc.stderr.strip() or proc.stdout.strip() or ""), dict(self.status)

        successes = 0
        errors = []
        active = [
            (ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports)
            if ip in self.ready_ips
        ]
        if not active:
            return False, "no ready links", dict(self.status)
        executor = ThreadPoolExecutor(max_workers=max(1, len(active)))
        futures = [
            executor.submit(self._run_link_command, ip, port, remote_command, None)
            for ip, port in active
        ]
        try:
            for future in as_completed(futures):
                ip, state, _output, error = future.result()
                if state == "ok":
                    self._mark_success(ip)
                    successes += 1
                    executor.shutdown(wait=False, cancel_futures=True)
                    return True, "", dict(self.status)
                self._mark_failure(ip, error)
                if state == "fail":
                    errors.append(f"{ip}: {error or 'send failed'}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if errors:
            self.last_error = "; ".join(errors)
        return successes > 0, "; ".join(errors), dict(self.status)

    def clear_messages(self) -> Tuple[bool, str]:
        remote_command = f"bash {self.remote_script} -c"
        if self.mode == "ssh":
            proc = self._remote_run(remote_command, timeout=REMOTE_COMMAND_TIMEOUT)
            return proc.returncode == 0, proc.stderr.strip()

        errors = []
        active = [
            (ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports)
            if ip in self.ready_ips
        ]
        if not active:
            return False, "no ready links"
        with ThreadPoolExecutor(max_workers=max(1, len(active))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
                for ip, port in active
            ]
            for future in as_completed(futures):
                ip, state, _output, error = future.result()
                if state == "ok":
                    self._mark_success(ip)
                    return True, ""
                self._mark_failure(ip, error)
                if state == "fail":
                    errors.append(f"{ip}: {error or 'clear failed'}")
        self.last_error = "; ".join(errors)
        return False, "clear failed"

    def fetch_news(self, channel: str, range_spec: str) -> Tuple[bool, str, Dict[str, str]]:
        normalized_range = self.normalize_news_range(range_spec)
        remote_command = f"bash {self.remote_script} -g {shlex.quote(channel)} {shlex.quote(normalized_range)}"

        if self.mode == "ssh":
            try:
                proc = self._remote_run(remote_command, timeout=NEWS_COMMAND_TIMEOUT)
            except Exception as exc:
                self._mark_failure("ssh", str(exc))
                self.last_error = str(exc)
                return False, str(exc), dict(self.status)
            if proc.returncode == 0:
                self._mark_success("ssh")
                return True, proc.stdout.strip() or "news imported", dict(self.status)
            err = proc.stderr.strip() or proc.stdout.strip() or "news fetch failed"
            self._mark_failure("ssh", err)
            self.last_error = err
            return False, err, dict(self.status)

        ordered_links = sorted(
            [(ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports) if ip in self.ready_ips],
            key=lambda item: {"ok": 0, "unknown": 1, "fail": 2}.get(self.status.get(item[0], "unknown"), 1),
        )
        if not ordered_links:
            return False, "no ready links", dict(self.status)
        errors = []
        for ip, port in ordered_links:
            link_ip, state, output, error = self._run_link_command(ip, port, remote_command, NEWS_COMMAND_TIMEOUT)
            if state == "ok":
                self._mark_success(link_ip)
                return True, output.strip() or "news imported", dict(self.status)
            self._mark_failure(link_ip, error)
            errors.append(f"{link_ip}: {error or 'news fetch failed'}")

        self.last_error = "; ".join(errors)
        return False, self.last_error or "news fetch failed", dict(self.status)

    def upload_file(
        self,
        local_path: str,
        progress_cb: Optional[Callable[[str, Optional[int], str], None]] = None,
    ) -> Tuple[bool, str, Dict[str, str]]:
        source = Path(local_path).expanduser()
        if not source.exists() or not source.is_file():
            return False, "file not found", dict(self.status)

        remote_dir = f"{self._remote_base_dir()}/uploads"
        remote_name = source.name
        mkdir_cmd = f'mkdir -p "{remote_dir.replace(chr(34), chr(92) + chr(34))}"'
        upload_timeout = FILE_TRANSFER_TIMEOUT

        if self.mode == "ssh":
            mkdir_proc = self._remote_run(mkdir_cmd, timeout=REMOTE_COMMAND_TIMEOUT)
            if mkdir_proc.returncode != 0:
                self._mark_failure("ssh", mkdir_proc.stderr.strip() or "mkdir failed")
                return False, mkdir_proc.stderr.strip() or "mkdir failed", dict(self.status)
            scp_cmd = self._base_scp_cmd() + [str(source), f"{self._remote_target()}:{remote_dir}/{remote_name}"]
            ok, error = self._run_scp_with_progress(scp_cmd, "ssh", upload_timeout, progress_cb=progress_cb)
            if ok:
                self._mark_success("ssh")
                return True, remote_name, dict(self.status)
            self._mark_failure("ssh", error or "upload failed")
            self.last_error = error or "upload failed"
            return False, self.last_error, dict(self.status)

        errors = []
        chosen_link: Optional[Tuple[str, int]] = None
        active = [
            (ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports)
            if ip in self.ready_ips
        ]
        if not active:
            return False, "no ready links", dict(self.status)
        if progress_cb:
            for ip, _port in active:
                progress_cb(ip, 0, "probing")

        with ThreadPoolExecutor(max_workers=max(1, len(active))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, mkdir_cmd, REMOTE_COMMAND_TIMEOUT)
                for ip, port in active
            ]
            for future in as_completed(futures):
                ip, state, _output, error = future.result()
                if state == "ok":
                    chosen_link = (ip, self.proxy_ports[self.dns_ips.index(ip)])
                    self._mark_success(ip)
                    if progress_cb:
                        progress_cb(ip, 0, "selected")
                    break
                self._mark_failure(ip, error)
                errors.append(f"{ip}: {error or 'mkdir failed'}")
                if progress_cb:
                    progress_cb(ip, None, "failed")

        if chosen_link is None:
            self.last_error = "; ".join(errors) or "no working upload link"
            return False, self.last_error, dict(self.status)

        chosen_ip, chosen_port = chosen_link
        scp_cmd = self._base_scp_cmd(proxy_port=chosen_port) + [
            str(source),
            f"{self._remote_target()}:{remote_dir}/{remote_name}",
        ]
        ok, error = self._run_scp_with_progress(
            scp_cmd,
            chosen_ip,
            upload_timeout,
            progress_cb=progress_cb,
        )
        if ok:
            self._mark_success(chosen_ip)
            return True, remote_name, dict(self.status)

        self._mark_failure(chosen_ip, error or "upload failed")
        errors.append(f"{chosen_ip}: {error or 'upload failed'}")
        self.last_error = "; ".join(errors) or "upload failed"
        return False, self.last_error, dict(self.status)

    def download_file(
        self,
        remote_relative_path: str,
        progress_cb: Optional[Callable[[str, Optional[int], str], None]] = None,
    ) -> Tuple[bool, str, Dict[str, str]]:
        remote_path = self._remote_file_path(remote_relative_path)
        local_dir = APP_RUNTIME_ROOT / "downloads"
        local_dir.mkdir(exist_ok=True)
        local_path = local_dir / Path(remote_relative_path).name
        check_cmd = f'test -f "{remote_path.replace(chr(34), chr(92) + chr(34))}"'
        download_timeout = FILE_TRANSFER_TIMEOUT

        if self.mode == "ssh":
            check_proc = self._remote_run(check_cmd, timeout=REMOTE_COMMAND_TIMEOUT)
            if check_proc.returncode != 0:
                self._mark_failure("ssh", "file not found")
                return False, "remote file not found", dict(self.status)
            scp_cmd = self._base_scp_cmd() + [f"{self._remote_target()}:{remote_path}", str(local_path)]
            ok, error = self._run_scp_with_progress(scp_cmd, "ssh", download_timeout, progress_cb=progress_cb)
            if ok:
                self._mark_success("ssh")
                return True, str(local_path), dict(self.status)
            self._mark_failure("ssh", error or "download failed")
            self.last_error = error or "download failed"
            return False, self.last_error, dict(self.status)

        errors = []
        chosen_link: Optional[Tuple[str, int]] = None
        active = [
            (ip, port) for ip, port in zip(self.dns_ips, self.proxy_ports)
            if ip in self.ready_ips
        ]
        if not active:
            return False, "no ready links", dict(self.status)
        if progress_cb:
            for ip, _port in active:
                progress_cb(ip, 0, "probing")

        with ThreadPoolExecutor(max_workers=max(1, len(active))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, check_cmd, REMOTE_COMMAND_TIMEOUT)
                for ip, port in active
            ]
            for future in as_completed(futures):
                ip, state, _output, error = future.result()
                if state == "ok":
                    chosen_link = (ip, self.proxy_ports[self.dns_ips.index(ip)])
                    self._mark_success(ip)
                    if progress_cb:
                        progress_cb(ip, 0, "selected")
                    break
                self._mark_failure(ip, error)
                errors.append(f"{ip}: {error or 'file not found'}")
                if progress_cb:
                    progress_cb(ip, None, "failed")

        if chosen_link is None:
            self.last_error = "; ".join(errors) or "no working download link"
            return False, self.last_error, dict(self.status)

        chosen_ip, chosen_port = chosen_link
        scp_cmd = self._base_scp_cmd(proxy_port=chosen_port) + [
            f"{self._remote_target()}:{remote_path}",
            str(local_path),
        ]
        ok, error = self._run_scp_with_progress(
            scp_cmd,
            chosen_ip,
            download_timeout,
            progress_cb=progress_cb,
        )
        if ok:
            self._mark_success(chosen_ip)
            return True, str(local_path), dict(self.status)

        self._mark_failure(chosen_ip, error or "download failed")
        errors.append(f"{chosen_ip}: {error or 'download failed'}")
        self.last_error = "; ".join(errors) or "download failed"
        return False, self.last_error, dict(self.status)


class StatusPanel(Static):
    @staticmethod
    def _format_age(age_seconds: Optional[int]) -> str:
        if age_seconds is None:
            return "never"
        if age_seconds < 60:
            return "now"
        if age_seconds < 3600:
            return f"{age_seconds // 60}m ago"
        if age_seconds < 86400:
            return f"{age_seconds // 3600}h ago"
        return f"{age_seconds // 86400}d ago"

    @staticmethod
    def _overall_state(statuses: Dict[str, str]) -> Tuple[str, str]:
        values = list(statuses.values())
        if any(state == "ok" for state in values):
            return "online", "green"
        if any(state == "unknown" for state in values):
            return "waiting", "yellow"
        return "offline", "red"

    def render_status(
        self,
        mode: str,
        statuses: Dict[str, str],
        last_error: str = "",
    ) -> None:
        lines = [f"[bold]Mode[/]: {mode}"]
        if not statuses:
            lines += ["", "[dim]No link status yet[/]"]
        else:
            overall, color = self._overall_state(statuses)
            lines += ["", f"[bold]State[/]: [{color}]{overall}[/{color}]"]
        if last_error:
            lines += ["", f"[bold]Error[/]", f"[red]{last_error}[/red]"]
        self.update("\n".join(lines))


class DNSLinkLabel(Static):
    """A clickable label for a DNS link. Failed links post a remove request on click."""

    def __init__(self, ip: str, is_fail: bool, label_text: str, **kwargs) -> None:
        super().__init__(label_text, **kwargs)
        self.ip = ip
        self.is_fail = is_fail

    def on_click(self) -> None:
        if self.is_fail:
            node = self.parent
            while node:
                if hasattr(node, "_remove_dns_link"):
                    asyncio.create_task(node._remove_dns_link(self.ip))
                    return
                node = node.parent


class UploadInput(Input):
    def _extract_file_path(self, text: str) -> Optional[str]:
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

    def as_upload_command(self, text: str) -> Optional[str]:
        file_path = self._extract_file_path(text)
        if not file_path:
            return None
        return f"/upload {file_path}"

    def _on_paste(self, event: events.Paste) -> None:
        upload_command = self.as_upload_command(event.text)
        if upload_command:
            self.value = upload_command
            self.post_message(self.Submitted(self, self.value, None))
            event.stop()
            return
        super()._on_paste(event)


class ChatView(Static):
    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        width: 100%;
        layout: vertical;
    }
    #main {
        height: 1fr;
        layout: horizontal;
    }
    #chat-column {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }
    #filter-bar {
        height: 3;
        width: 100%;
        padding: 0 1;
        dock: top;
    }
    .filter-btn {
        min-width: 10;
        height: 3;
        margin: 0 1 0 0;
        background: $surface-darken-1;
        color: $text-muted;
        border: tall $primary-darken-2;
    }
    .filter-btn:hover {
        background: $primary-darken-1;
        color: $text;
    }
    .filter-active {
        background: $primary;
        color: $text;
        border: tall $primary;
    }
    #chat-area {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        border: solid $primary;
    }
    #sidebar {
        width: 30;
        height: 1fr;
        padding: 0;
        border: solid $primary;
        background: $surface-darken-1;
    }
    #status {
        height: auto;
        padding: 1;
    }
    #links-header {
        height: auto;
        padding: 0 1;
    }
    #dns-btns {
        height: auto;
        layout: vertical;
        padding: 0;
    }
    .dns-link {
        height: auto;
        width: 1fr;
        padding: 0 1;
    }
    .dns-link-fail {
        color: red;
    }
    .dns-link-fail:hover {
        background: darkred;
        color: white;
    }
    .dns-link-ok {
        color: green;
    }
    .dns-link-unknown {
        color: yellow;
    }
    #error-text {
        height: auto;
        padding: 0 1;
    }
    #scan-btn {
        height: 3;
        min-width: 10;
        width: auto;
        margin: 1 0 0 0;
        background: darkcyan;
        color: white;
    }
    #scan-btn:hover {
        background: cyan;
    }
    #scan-status {
        height: auto;
        padding: 0 1;
    }
    #input-area {
        height: auto;
        layout: vertical;
        padding: 1 2;
        border: solid $primary;
    }
    #transfer-status {
        height: auto;
        min-height: 1;
        padding: 0 0 1 0;
    }
    """

    def __init__(
        self,
        transport: ChatTransport,
        display_name: str,
        startup_lines: Optional[List[str]] = None,
        scanner_input_file: str = "",
        on_new_dns_ip: Optional[Callable[[str, int], None]] = None,
        on_restart_link: Optional[Callable[[str, int], None]] = None,
    ):
        super().__init__()
        self.transport = transport
        self.display_name = display_name or os.environ.get("USER", "anon")
        self.startup_lines = startup_lines or []
        self.last_snapshot = ""
        self.poll_task: Optional[asyncio.Task] = None
        self.pending_messages: List[Tuple[str, str]] = []
        self.upload_status: Dict[str, str] = {}
        self.available_files: Dict[str, str] = {}
        self.seen_msg_ids: set = set()
        self.scanner_input_file = scanner_input_file
        self._scanner_proc: Optional[subprocess.Popen] = None
        self._scanner_output_path = APP_RUNTIME_ROOT / "scanner-result.txt"
        self._scanner_known_ips: set = set()
        self._scan_finished_at: Optional[float] = None
        self._scan_timer = None
        self._on_new_dns_ip = on_new_dns_ip
        self._on_restart_link = on_restart_link
        self._message_filter_mode = "all"

    def _has_rtl(self, text: str) -> bool:
        for c in (text or ""):
            if "\u0590" <= c <= "\u08FF" or "\uFB50" <= c <= "\uFDFF" or "\uFE70" <= c <= "\uFEFF":
                return True
        return False

    def _write_message(self, header_markup: str, body_text: str, pending: bool = False) -> None:
        """Write a chat message to the chat area.

        For RTL text, renders WITHOUT Panel borders to avoid terminal bidi
        reordering the box-drawing characters and pushing content outside
        the visible area.  Uses a dim rule + header + plain text instead.

        For LTR text, uses the original Panel-based rendering.
        """
        if self._has_rtl(body_text):
            self._write_rtl_message(header_markup, body_text, pending)
        else:
            body = Text.from_markup(body_text)
            if pending:
                body.append(" (pending)", style="yellow")
            self.chat_area.write(
                Panel(
                    Group(Text.from_markup(header_markup), body),
                    padding=(0, 1),
                    border_style="dim",
                    box=box.ROUNDED,
                )
            )

    def _rtl_content_width(self) -> int:
        """Max chars per line for RTL text inside the chat area (no Panel)."""
        # Overhead: status panel (28) + chat border (2) + chat padding (4)
        #         + scrollbar (2) + safety (4) = 40
        try:
            return max(20, self.app.size.width - 40)
        except Exception:
            return 60

    def _write_rtl_message(self, header_markup: str, body_text: str, pending: bool = False) -> None:
        """Render an RTL message without Panel borders.

        Three-part fix for terminal bidi overflow:
        1. NO Panel borders  – removes box-drawing chars that bidi reorders
        2. LRM per line       – forces LTR paragraph direction so terminal
                                does not right-align to terminal edge
        3. Pre-wrap to width  – ensures each line fits the chat area; Rich
                                won't re-wrap and lose the LRM anchors
        """
        import textwrap
        LRM = "\u200E"
        max_w = self._rtl_content_width()

        # Separator
        self.chat_area.write(Rule(style="dim"))
        # Header (LRM-anchored so it also stays in place)
        self.chat_area.write(Text.from_markup(LRM + header_markup))

        # Body – pre-wrap each paragraph, anchor every line with LRM
        plain = Text.from_markup(body_text).plain
        lines: list[str] = []
        for para in plain.split("\n"):
            if para.strip():
                for line in textwrap.fill(para, width=max_w).split("\n"):
                    lines.append(LRM + line)
            else:
                lines.append("")

        body = Text("\n".join(lines))
        if pending:
            body.append(" (pending)", style="yellow")
        self.chat_area.write(body)

    def _scanner_status_text(self) -> str:
        if self.transport.mode != "dns":
            return ""
        if self._scanner_proc and self._scanner_proc.poll() is None:
            return "Scanning..."
        if self._scanner_proc and self._scan_finished_at is not None:
            elapsed = max(0, int(time.time() - self._scan_finished_at))
            if elapsed < 60:
                age = "just now"
            elif elapsed < 3600:
                age = f"{elapsed // 60}m ago"
            else:
                age = f"{elapsed // 3600}h ago"
            return f"Scanned: {age} ({len(self._scanner_known_ips)} found)"
        return ""

    def _start_scan(self, input_file: str = "") -> None:
        if self.transport.mode != "dns":
            self.write_system("[red]Scan only available in DNS mode[/]")
            return
        if self._scanner_proc and self._scanner_proc.poll() is None:
            self.write_system("[yellow]Scanner already running...[/]")
            return
        input_path = Path(input_file or self.scanner_input_file).expanduser()
        if not input_path.exists():
            self.write_system("[red]Scanner input file not found. Use /scan <path>[/]")
            return
        scanner_script = APP_RESOURCE_ROOT / "scanner.py"
        if not scanner_script.exists():
            self.write_system("[red]scanner.py not found[/]")
            return
        self._scanner_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._scanner_output_path.write_text("")
        self._scanner_known_ips.clear()
        self._scan_finished_at = None
        self._scanner_proc = subprocess.Popen(
            [sys.executable, str(scanner_script), "-f", str(input_path), "-o", str(self._scanner_output_path)],
            cwd=str(APP_RESOURCE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.write_system("[yellow]Scanner started...[/]")
        self._render_status_panel(self.transport.status)
        if self._scan_timer:
            self._scan_timer.resume()

    def _poll_scanner(self) -> None:
        if self._scanner_output_path.exists():
            new_found = False
            for line in self._scanner_output_path.read_text().splitlines():
                if "IP:" in line and "Time:" in line:
                    parts = line.split("IP:")[1].strip().split("-")
                    ip = parts[0].strip()
                    if ip and ip not in self._scanner_known_ips:
                        self._scanner_known_ips.add(ip)
                        if ip not in self.transport.dns_ips:
                            self._add_scanner_ip(ip)
                            new_found = True
            if new_found:
                self._render_status_panel(self.transport.status)
        if self._scanner_proc and self._scanner_proc.poll() is not None:
            if self._scan_finished_at is None:
                self._scan_finished_at = time.time()
                self.write_system(f"[green]Scan complete[/]: {len(self._scanner_known_ips)} IPs found")
            self._render_status_panel(self.transport.status)
        if not self._scanner_proc or self._scanner_proc.poll() is not None:
            if self._scan_timer and self._scan_finished_at:
                self._scan_timer.pause()

    def _add_scanner_ip(self, ip: str) -> None:
        max_port = max(self.transport.proxy_ports) if self.transport.proxy_ports else 7999
        new_port = max_port + 1
        self.transport.dns_ips.append(ip)
        self.transport.proxy_ports.append(new_port)
        self.transport.status[ip] = "unknown"
        self.transport.fail_counts[ip] = 0
        self.transport.last_online_at[ip] = None
        self.write_system(f"[green]Scanner found:[/] {ip}")
        if self._on_new_dns_ip:
            self._on_new_dns_ip(ip, new_port)

    def _play_notification_sound(self) -> None:
        def _run() -> None:
            try:
                if sys.platform == "darwin":
                    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=1, capture_output=True, check=False)
                elif shutil.which("paplay"):
                    subprocess.run(["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"], timeout=1, capture_output=True, check=False)
                elif shutil.which("aplay"):
                    subprocess.run(["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"], timeout=1, capture_output=True, check=False)
                else:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _append_local_line(self, user: str, text: str, pending: bool = False) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        display_text, _ = self._parse_file_message(text)
        header = f"[dim]{now}[/] [bold]{user}[/]"
        self._write_message(header, display_text, pending=pending)

    def write_system(self, text: str, style: str = "dim") -> None:
        self.chat_area.write(f"[{style}]{text}[/{style}]")

    def _parse_file_message(self, text: str) -> Tuple[str, Optional[Tuple[str, str]]]:
        file_match = re.match(r"^\[file\]\s+(.+?)::(.+)$", text)
        if file_match:
            name, relative_path = file_match.groups()
            return (
                f"[file] [u cyan]{escape(name)}[/u cyan] [dim](/download {escape(name)})[/dim]",
                (name, relative_path),
            )

        tg_match = re.match(r"^\[TG file\]\s+(.+?)\s+saved to\s+(.+)$", text)
        if tg_match:
            name, saved_name = tg_match.groups()
            return (
                f"[file] [u cyan]{escape(name)}[/u cyan] [dim](/download {escape(name)})[/dim]",
                (name, f"tg_files/{saved_name}"),
            )

        return text, None

    def compose(self) -> ComposeResult:
        with Container(id="main"):
            with VerticalScroll(id="sidebar"):
                yield StatusPanel(id="status")
                yield Static("[bold]Links[/]", id="links-header")
                yield Vertical(id="dns-btns")
                yield Static("", id="error-text")
                yield Button("Scan", id="scan-btn")
                yield Static("", id="scan-status")
            with Vertical(id="chat-column"):
                with Horizontal(id="filter-bar"):
                    yield Button("All", id="filter-all", classes="filter-btn filter-active")
                    yield Button("News", id="filter-news", classes="filter-btn")
                    yield Button("Messages", id="filter-messages", classes="filter-btn")
                yield RichLog(id="chat-area", wrap=True, markup=True)
        with Container(id="input-area"):
            yield Static("", id="transfer-status")
            yield UploadInput(placeholder="Type message. Commands: /clear /upload /download /news", id="msg-input")

    def on_mount(self) -> None:
        self.chat_area = self.query_one("#chat-area", RichLog)
        self.status_panel = self.query_one("#status", StatusPanel)
        self.transfer_status = self.query_one("#transfer-status", Static)
        self.input_w = self.query_one("#msg-input", Input)
        self.dns_btns_container = self.query_one("#dns-btns", Vertical)
        self.scan_btn = self.query_one("#scan-btn", Button)
        self.scan_status_w = self.query_one("#scan-status", Static)
        self.error_text_w = self.query_one("#error-text", Static)
        self.links_header_w = self.query_one("#links-header", Static)
        self._last_link_snapshot: str = ""
        if self.transport.mode != "dns":
            self.scan_btn.display = False
            self.dns_btns_container.display = False
            self.scan_status_w.display = False
            self.links_header_w.display = False
            self.error_text_w.display = False
        for line in self.startup_lines:
            self.write_system(line)
        self._render_status_panel(self.transport.status)
        asyncio.create_task(self.refresh_now())
        self.poll_task = asyncio.create_task(self._poll_loop())
        self._scan_timer = self.set_interval(2, self._poll_scanner, pause=True)

    def _render_status_panel(self, statuses: Dict[str, str]) -> None:
        is_dns = self.transport.mode == "dns"
        if is_dns:
            live = set(self.transport.dns_ips)
            statuses = {k: v for k, v in statuses.items() if k in live}
        self.status_panel.render_status(
            self.transport.mode.upper(),
            statuses,
            "" if is_dns else self.transport.last_error,
        )
        if is_dns:
            ages = {label: self.transport.last_online_age_seconds(label) for label in statuses}
            self._update_link_buttons(statuses, ages)
            err = self.transport.last_error
            self.error_text_w.update(f"[red]{err}[/red]" if err else "")
            scanner_text = self._scanner_status_text()
            self.scan_status_w.update(f"[dim]{scanner_text}[/dim]" if scanner_text else "")

    def _update_link_buttons(self, statuses: Dict[str, str], ages: Dict[str, Optional[int]]) -> None:
        # Build a snapshot string to detect changes
        snapshot = "|".join(f"{ip}:{st}:{ages.get(ip)}" for ip, st in statuses.items())
        if snapshot == self._last_link_snapshot:
            return
        self._last_link_snapshot = snapshot
        # Remove old labels
        for child in list(self.dns_btns_container.children):
            child.remove()
        # Create a label for each link
        for ip, state in statuses.items():
            age_text = StatusPanel._format_age(ages.get(ip))
            is_fail = state == "fail"
            if is_fail:
                text = f"[red][x] {ip}: {age_text}[/red]"
            elif state == "ok":
                text = f"[green]{ip}: {age_text}[/green]"
            else:
                text = f"[yellow]{ip}: {age_text}[/yellow]"
            state_cls = f"dns-link-{state}" if state in ("fail", "ok", "unknown") else "dns-link-unknown"
            lbl = DNSLinkLabel(ip, is_fail, text, classes=f"dns-link {state_cls}")
            self.dns_btns_container.mount(lbl)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-btn":
            self._start_scan()
        elif event.button.id in ("filter-all", "filter-news", "filter-messages"):
            self._set_message_filter(event.button.id.replace("filter-", ""))

    def _set_message_filter(self, mode: str) -> None:
        self._message_filter_mode = mode
        for btn_id, btn_mode in (("filter-all", "all"), ("filter-news", "news"), ("filter-messages", "messages")):
            btn = self.query_one(f"#{btn_id}", Button)
            btn.set_classes("filter-btn filter-active" if btn_mode == mode else "filter-btn")
        if self.last_snapshot:
            self._render_snapshot(self.last_snapshot)

    async def refresh_now(self) -> None:
        try:
            snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
            self._render_status_panel(statuses)
            if snapshot is not None:
                self.last_snapshot = snapshot
                self._render_snapshot(snapshot)
        except Exception as exc:
            self.transport.last_error = str(exc)
            self._render_status_panel(self.transport.status)

    def _restart_pending_links(self) -> None:
        """Restart slipstream processes for DNS links that got 'unknown port 65535'."""
        pending = self.transport.pop_pending_restarts()
        if not pending or not self._on_restart_link:
            return
        for ip in pending:
            if ip not in self.transport.dns_ips:
                continue
            idx = self.transport.dns_ips.index(ip)
            port = self.transport.proxy_ports[idx]
            retry = self.transport.retry_counts.get(ip, 0)
            self.write_system(
                f"[yellow]Retrying DNS link {ip} ({retry}/2)[/yellow]"
            )
            self._on_restart_link(ip, port)

    async def _poll_loop(self) -> None:
        while True:
            try:
                snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
                self._render_status_panel(statuses)
                if snapshot is not None and snapshot != self.last_snapshot:
                    self.last_snapshot = snapshot
                    self._render_snapshot(snapshot)
                # When SSH is back online, retry all pending messages
                if (
                    snapshot is not None
                    and self.pending_messages
                    and self.transport.mode == "ssh"
                ):
                    await self._retry_all_pending()
                self._restart_pending_links()
            except Exception as exc:
                self.transport.last_error = str(exc)
                self._render_status_panel(self.transport.status)
            await asyncio.sleep(STATUS_POLL_INTERVAL)

    def _should_show_message(self, user: str) -> bool:
        if self._message_filter_mode == "all":
            return True
        is_news = user.startswith("news/")
        if self._message_filter_mode == "news":
            return is_news
        return not is_news  # "messages" mode

    def _render_snapshot(self, snapshot: str) -> None:
        self.chat_area.clear()
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
                    display_text, file_entry = self._parse_file_message(text)
                    if self._should_show_message(user):
                        header = f"[dim]{ts}[/] [bold]{user}[/]"
                        self._write_message(header, display_text)
                    if file_entry:
                        name, relative_path = file_entry
                        available_files[name] = relative_path
                    continue
            self.chat_area.write(line)
        for user, text in self.pending_messages:
            self._append_local_line(user, text, pending=True)
        self.available_files = available_files
        if should_play:
            self._play_notification_sound()

    def _resolve_download_target(self, query: str) -> Optional[Tuple[str, str]]:
        query = query.strip()
        if not query:
            return None
        if query in self.available_files:
            return query, self.available_files[query]
        for name, relative_path in self.available_files.items():
            if relative_path == query or relative_path.endswith(query):
                return name, relative_path
        return None

    def _render_upload_status(self) -> None:
        if not self.upload_status:
            self.transfer_status.update("")
            return
        percent_items = [(label, status) for label, status in self.upload_status.items() if status.endswith("%")]
        if percent_items:
            label, status = sorted(percent_items, key=lambda item: int(item[1][:-1]), reverse=True)[0]
            self.transfer_status.update(f"[bold]Transfer[/]: {label} {status}")
            return
        selected_items = [(label, status) for label, status in self.upload_status.items() if status == "selected"]
        if selected_items:
            label, _status = selected_items[0]
            self.transfer_status.update(f"[bold]Transfer[/]: {label} selected")
            return
        parts = [f"{label}: {status}" for label, status in sorted(self.upload_status.items())]
        self.transfer_status.update("[bold]Transfer[/]: " + " | ".join(parts[:3]))

    def _update_upload_status(self, label: str, percent: Optional[int], stage: str) -> None:
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
        self._render_upload_status()

    def _clear_upload_status(self) -> None:
        self.upload_status.clear()
        self._render_upload_status()

    async def _send_text(self, text: str) -> None:
        self.pending_messages.append((self.display_name, text))
        self._render_snapshot(self.last_snapshot)
        asyncio.create_task(self._send_text_background(text))

    async def _retry_all_pending(self) -> None:
        """Send all pending messages (SSH mode). Used when back online."""
        if not self.pending_messages or self.transport.mode != "ssh":
            return
        # Snapshot the queue so we can iterate safely
        to_retry = list(self.pending_messages)
        for user, text in to_retry:
            preview = text if len(text) <= 30 else text[:27] + "..."
            self.write_system(f"[yellow]Retrying pending message: {escape(preview)}[/yellow]")
            ok, err, statuses = await asyncio.to_thread(
                self.transport.send_message, user, text
            )
            self._render_status_panel(statuses)
            if not ok:
                self.write_system(f"[red]Retry failed: {escape(err or 'no working link')}[/red]")
                return  # stop draining — link may be down again
            self.write_system(f"[green]Retry succeeded[/green]")
            try:
                self.pending_messages.remove((user, text))
            except ValueError:
                pass
        snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
        self._render_status_panel(statuses)
        if snapshot is not None:
            self.last_snapshot = snapshot
            self._render_snapshot(snapshot)
        else:
            self._render_snapshot(self.last_snapshot)

    async def _send_text_background(self, text: str) -> None:
        ok, err, statuses = await asyncio.to_thread(self.transport.send_message, self.display_name, text)
        self._render_status_panel(statuses)
        if not ok:
            self.chat_area.write(f"[red]Send failed[/]: {err or 'no working link'}")
            return
        try:
            self.pending_messages.remove((self.display_name, text))
        except ValueError:
            pass
        snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
        self._render_status_panel(statuses)
        if snapshot is not None:
            self.last_snapshot = snapshot
            self._render_snapshot(snapshot)
        else:
            self._render_snapshot(self.last_snapshot)

    async def _clear_chat(self) -> None:
        ok, err = await asyncio.to_thread(self.transport.clear_messages)
        if ok:
            self.last_snapshot = ""
            self.seen_msg_ids.clear()
            self.chat_area.clear()
            self.chat_area.write("[yellow]Chat cleared[/]")
        else:
            self.chat_area.write(f"[red]Clear failed[/]: {err}")

    async def _fetch_news(self, channel: str, range_spec: str) -> None:
        self.chat_area.write(f"[yellow]Fetching news[/]: {channel} {range_spec}")
        ok, result, statuses = await asyncio.to_thread(self.transport.fetch_news, channel, range_spec)
        self._render_status_panel(statuses)
        if not ok:
            self.chat_area.write(f"[red]News failed[/]: {result}")
            return
        self.chat_area.write(f"[green]News imported[/]: {channel} {range_spec}")
        await self.refresh_now()

    async def _upload_file(self, path: str) -> None:
        self.chat_area.write(f"[yellow]Uploading[/]: {path}")
        self._clear_upload_status()

        def progress_cb(label: str, percent: Optional[int], stage: str) -> None:
            self.app.call_from_thread(self._update_upload_status, label, percent, stage)

        ok, result, statuses = await asyncio.to_thread(self.transport.upload_file, path, progress_cb)
        self._render_status_panel(statuses)
        if not ok:
            self._clear_upload_status()
            self.chat_area.write(f"[red]Upload failed[/]: {result}")
            return
        self._clear_upload_status()
        self.chat_area.write(f"[green]Upload complete[/]: {result}")
        await self._send_text(f"[file] {result}::uploads/{result}")

    async def _download_file(self, name: str, relative_path: str) -> None:
        self.chat_area.write(f"[yellow]Downloading[/]: {name}")
        self._clear_upload_status()

        def progress_cb(label: str, percent: Optional[int], stage: str) -> None:
            self.app.call_from_thread(self._update_upload_status, label, percent, stage)

        ok, result, statuses = await asyncio.to_thread(self.transport.download_file, relative_path, progress_cb)
        self._render_status_panel(statuses)
        self._clear_upload_status()
        if not ok:
            self.chat_area.write(f"[red]Download failed[/]: {result}")
            return
        self.chat_area.write(f"[green]Downloaded[/]: {name} -> {result}")

    async def _remove_dns_link(self, ip: str) -> None:
        if self.transport.mode != "dns":
            self.chat_area.write("[red]DNS remove failed[/]: only available in DNS mode")
            return
        state = self.transport.status.get(ip, "unknown")
        if state != "fail":
            self.chat_area.write("[yellow]DNS remove skipped[/]: only offline links can be removed")
            return
        if not self.transport.remove_dns_link(ip):
            self.chat_area.write(f"[red]DNS remove failed[/]: {ip} not found")
            return
        self.chat_area.write(f"[green]Removed offline DNS link[/]: {ip}")
        self._render_status_panel(self.transport.status)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if isinstance(event.input, UploadInput) and not text.startswith("/upload "):
            upload_command = event.input.as_upload_command(text)
            if upload_command:
                text = upload_command
        if text == "/clear":
            await self._clear_chat()
            return
        if text.startswith("/news "):
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                self.chat_area.write("[red]News failed[/]: usage /news ChannelName 10 or /news ChannelName 20-10 or /news ChannelName 20 10")
                return
            channel = parts[1].strip()
            range_spec = parts[2].strip() if len(parts) > 2 else "10"
            if not channel:
                self.chat_area.write("[red]News failed[/]: channel name required")
                return
            await self._fetch_news(channel, range_spec)
            return
        if text.startswith("/upload "):
            await self._upload_file(text.split(" ", 1)[1].strip())
            return
        if text.startswith("/download "):
            target = self._resolve_download_target(text.split(" ", 1)[1].strip())
            if not target:
                self.chat_area.write("[red]Download failed[/]: file not found in recent messages")
                return
            name, relative_path = target
            await self._download_file(name, relative_path)
            return
        if text.startswith("/dns-remove "):
            ip = text.split(" ", 1)[1].strip()
            if not ip:
                self.chat_area.write("[red]DNS remove failed[/]: usage /dns-remove <ip>")
                return
            await self._remove_dns_link(ip)
            return
        if text.startswith("/scan"):
            parts = text.split(maxsplit=1)
            input_file = parts[1].strip() if len(parts) > 1 else ""
            self._start_scan(input_file)
            return
        await self._send_text(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not isinstance(event.input, UploadInput):
            return
        if event.value.startswith("/upload "):
            return
        upload_command = event.input.as_upload_command(event.value)
        if upload_command and event.input.value != upload_command:
            event.input.value = upload_command
            event.input.cursor_position = len(upload_command)

    def shutdown(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        if self._scan_timer:
            self._scan_timer.pause()
        if self._scanner_proc and self._scanner_proc.poll() is None:
            self._scanner_proc.terminate()


class ChatApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, transport: ChatTransport, display_name: str, scanner_input_file: str = ""):
        super().__init__()
        self.transport = transport
        self.display_name = display_name or os.environ.get("USER", "anon")
        self.scanner_input_file = scanner_input_file
        self.chat_view: Optional[ChatView] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.chat_view = ChatView(self.transport, self.display_name, scanner_input_file=self.scanner_input_file)
        yield self.chat_view
        yield Footer()

    def action_quit(self) -> None:
        if self.chat_view:
            self.chat_view.shutdown()
        self.exit()

    async def action_download_file(self, name: str, relative_path: str) -> None:
        if self.chat_view:
            await self.chat_view._download_file(name, relative_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["ssh", "dns"], required=True)
    parser.add_argument("--host", default="")
    parser.add_argument("--domain", default="")
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-pass", required=True)
    parser.add_argument("--remote-script", default="~/chat-over-dnstt/chat.sh")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--proxy-ports", default="")
    parser.add_argument("--dns-ips", default="")
    parser.add_argument("--scanner-input", default="")
    args = parser.parse_args()

    proxy_ports = [int(item.strip()) for item in args.proxy_ports.split(",") if item.strip()]
    dns_ips = [item.strip() for item in args.dns_ips.split(",") if item.strip()]

    transport = ChatTransport(
        mode=args.mode,
        host=args.host,
        domain=args.domain,
        ssh_user=args.ssh_user,
        ssh_pass=args.ssh_pass,
        remote_script=args.remote_script,
        proxy_ports=proxy_ports,
        dns_ips=dns_ips,
    )
    app = ChatApp(transport=transport, display_name=args.display_name or args.ssh_user, scanner_input_file=args.scanner_input)
    app.run()


if __name__ == "__main__":
    main()
