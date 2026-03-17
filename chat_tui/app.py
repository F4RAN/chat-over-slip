"""Simple chat TUI backed by SSH commands."""
from __future__ import annotations

import argparse
import asyncio
import os
import pty
import re
import select
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual import events
from textual.widgets import Footer, Header, Input, RichLog, Static

# Tunable parameters for poor or unstable networks.
SSH_CONNECT_TIMEOUT = 45
SSH_SERVER_ALIVE_INTERVAL = 15
SSH_SERVER_ALIVE_COUNT_MAX = 2
REMOTE_COMMAND_TIMEOUT = 60
FILE_TRANSFER_TIMEOUT = 180
SCP_PROGRESS_POLL_INTERVAL = 0.25
STATUS_POLL_INTERVAL = 3
SOFT_ERROR_OK_GRACE_FAILURES = 3
SOFT_ERROR_UNKNOWN_GRACE_FAILURES = 2
APP_RUNTIME_ROOT = (
    Path(sys.executable).resolve().parent
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
        else:
            self.status = {"ssh": "unknown"}
            self.fail_counts = {"ssh": 0}
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

    def _mark_success(self, label: str) -> None:
        self.status[label] = "ok"
        self.fail_counts[label] = 0

    def _mark_failure(self, label: str, error: str) -> None:
        self.fail_counts[label] = self.fail_counts.get(label, 0) + 1
        if self._soft_error(error):
            if self.status.get(label) == "ok" and self.fail_counts[label] < SOFT_ERROR_OK_GRACE_FAILURES:
                self.status[label] = "ok"
            elif self.fail_counts[label] < SOFT_ERROR_UNKNOWN_GRACE_FAILURES:
                self.status[label] = "unknown"
            else:
                self.status[label] = "fail"
        else:
            self.status[label] = "fail"

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
        with ThreadPoolExecutor(max_workers=max(1, len(self.proxy_ports))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
                for ip, port in zip(self.dns_ips, self.proxy_ports)
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
            try:
                proc = self._remote_run(remote_command, timeout=None)
            except Exception as exc:
                self._mark_failure("ssh", str(exc))
                self.last_error = str(exc)
                return False, str(exc), dict(self.status)
            if proc.returncode == 0:
                self._mark_success("ssh")
            else:
                self._mark_failure("ssh", proc.stderr.strip() or proc.stdout.strip() or "send failed")
                self.last_error = proc.stderr.strip() or proc.stdout.strip() or "send failed"
            return proc.returncode == 0, proc.stderr.strip(), dict(self.status)

        successes = 0
        errors = []
        executor = ThreadPoolExecutor(max_workers=max(1, len(self.proxy_ports)))
        futures = [
            executor.submit(self._run_link_command, ip, port, remote_command, None)
            for ip, port in zip(self.dns_ips, self.proxy_ports)
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
        with ThreadPoolExecutor(max_workers=max(1, len(self.proxy_ports))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
                for ip, port in zip(self.dns_ips, self.proxy_ports)
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
        if progress_cb:
            for ip in self.dns_ips:
                progress_cb(ip, 0, "probing")

        with ThreadPoolExecutor(max_workers=max(1, len(self.proxy_ports))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, mkdir_cmd, REMOTE_COMMAND_TIMEOUT)
                for ip, port in zip(self.dns_ips, self.proxy_ports)
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
        if progress_cb:
            for ip in self.dns_ips:
                progress_cb(ip, 0, "probing")

        with ThreadPoolExecutor(max_workers=max(1, len(self.proxy_ports))) as executor:
            futures = [
                executor.submit(self._run_link_command, ip, port, check_cmd, REMOTE_COMMAND_TIMEOUT)
                for ip, port in zip(self.dns_ips, self.proxy_ports)
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
    def _overall_state(self, statuses: Dict[str, str]) -> Tuple[str, str]:
        values = list(statuses.values())
        if any(state == "ok" for state in values):
            return "online", "green"
        if any(state == "unknown" for state in values):
            return "waiting", "yellow"
        return "offline", "red"

    def render_status(self, mode: str, statuses: Dict[str, str], last_error: str = "") -> None:
        lines = [f"[bold]Mode[/]: {mode}"]
        if not statuses:
            lines += ["", "[dim]No link status yet[/]"]
        else:
            overall, color = self._overall_state(statuses)
            lines += ["", f"[bold]State[/]: [{color}]{overall}[/{color}]"]
            lines += ["", "[bold]Links[/]"]
            for label, state in statuses.items():
                color = "yellow" if state == "unknown" else ("green" if state == "ok" else "red")
                lines.append(f"[{color}]{label}: {state}[/{color}]")
        if last_error:
            lines += ["", "[bold]Last Error[/]", f"[red]{last_error}[/red]"]
        self.update("\n".join(lines))


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
    #chat-area {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        border: solid $primary;
    }
    #status {
        width: 28;
        height: 1fr;
        padding: 1;
        border: solid $primary;
        background: $surface-darken-1;
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

    def __init__(self, transport: ChatTransport, display_name: str, startup_lines: Optional[List[str]] = None):
        super().__init__()
        self.transport = transport
        self.display_name = display_name or os.environ.get("USER", "anon")
        self.startup_lines = startup_lines or []
        self.last_snapshot = ""
        self.poll_task: Optional[asyncio.Task] = None
        self.pending_messages: List[Tuple[str, str]] = []
        self.upload_status: Dict[str, str] = {}
        self.available_files: Dict[str, str] = {}

    def _append_local_line(self, user: str, text: str, pending: bool = False) -> None:
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        suffix = " [yellow](pending)[/yellow]" if pending else ""
        display_text, _ = self._parse_file_message(text)
        self.chat_area.write(f"[dim]{now}[/] [bold]{user}[/]: {display_text}{suffix}")

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
            yield RichLog(id="chat-area", wrap=True, markup=True)
            yield StatusPanel(id="status")
        with Container(id="input-area"):
            yield Static("", id="transfer-status")
            yield UploadInput(placeholder="Type message and press Enter. Commands: /clear /upload /path/file", id="msg-input")

    def on_mount(self) -> None:
        self.chat_area = self.query_one("#chat-area", RichLog)
        self.status_panel = self.query_one("#status", StatusPanel)
        self.transfer_status = self.query_one("#transfer-status", Static)
        self.input_w = self.query_one("#msg-input", Input)
        for line in self.startup_lines:
            self.write_system(line)
        self.status_panel.render_status(self.transport.mode.upper(), self.transport.status, self.transport.last_error)
        asyncio.create_task(self.refresh_now())
        self.poll_task = asyncio.create_task(self._poll_loop())

    async def refresh_now(self) -> None:
        try:
            snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
            self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
            if snapshot is not None:
                self.last_snapshot = snapshot
                self._render_snapshot(snapshot)
        except Exception as exc:
            self.transport.last_error = str(exc)
            self.status_panel.render_status(self.transport.mode.upper(), self.transport.status, self.transport.last_error)

    async def _poll_loop(self) -> None:
        while True:
            try:
                snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
                self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
                if snapshot is not None and snapshot != self.last_snapshot:
                    self.last_snapshot = snapshot
                    self._render_snapshot(snapshot)
            except Exception as exc:
                self.transport.last_error = str(exc)
                self.status_panel.render_status(self.transport.mode.upper(), self.transport.status, self.transport.last_error)
            await asyncio.sleep(STATUS_POLL_INTERVAL)

    def _render_snapshot(self, snapshot: str) -> None:
        self.chat_area.clear()
        available_files: Dict[str, str] = {}
        for line in snapshot.splitlines():
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    ts, _, user, text = parts
                    display_text, file_entry = self._parse_file_message(text)
                    self.chat_area.write(f"[dim]{ts}[/] [bold]{user}[/]: {display_text}")
                    if file_entry:
                        name, relative_path = file_entry
                        available_files[name] = relative_path
                    continue
            self.chat_area.write(line)
        for user, text in self.pending_messages:
            self._append_local_line(user, text, pending=True)
        self.available_files = available_files

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

    async def _send_text_background(self, text: str) -> None:
        ok, err, statuses = await asyncio.to_thread(self.transport.send_message, self.display_name, text)
        self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
        if not ok:
            self.chat_area.write(f"[red]Send failed[/]: {err or 'no working link'}")
            return
        try:
            self.pending_messages.remove((self.display_name, text))
        except ValueError:
            pass
        snapshot, statuses = await asyncio.to_thread(self.transport.read_messages, 200)
        self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
        if snapshot is not None:
            self.last_snapshot = snapshot
            self._render_snapshot(snapshot)
        else:
            self._render_snapshot(self.last_snapshot)

    async def _clear_chat(self) -> None:
        ok, err = await asyncio.to_thread(self.transport.clear_messages)
        if ok:
            self.last_snapshot = ""
            self.chat_area.clear()
            self.chat_area.write("[yellow]Chat cleared[/]")
        else:
            self.chat_area.write(f"[red]Clear failed[/]: {err}")

    async def _upload_file(self, path: str) -> None:
        self.chat_area.write(f"[yellow]Uploading[/]: {path}")
        self._clear_upload_status()

        def progress_cb(label: str, percent: Optional[int], stage: str) -> None:
            self.app.call_from_thread(self._update_upload_status, label, percent, stage)

        ok, result, statuses = await asyncio.to_thread(self.transport.upload_file, path, progress_cb)
        self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
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
        self.status_panel.render_status(self.transport.mode.upper(), statuses, self.transport.last_error)
        self._clear_upload_status()
        if not ok:
            self.chat_area.write(f"[red]Download failed[/]: {result}")
            return
        self.chat_area.write(f"[green]Downloaded[/]: {name} -> {result}")

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


class ChatApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, transport: ChatTransport, display_name: str):
        super().__init__()
        self.transport = transport
        self.display_name = display_name or os.environ.get("USER", "anon")
        self.chat_view: Optional[ChatView] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.chat_view = ChatView(self.transport, self.display_name)
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
    app = ChatApp(transport=transport, display_name=args.display_name or args.ssh_user)
    app.run()


if __name__ == "__main__":
    main()
