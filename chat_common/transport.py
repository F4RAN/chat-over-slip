"""Transport and file-transfer services shared by desktop frontends."""

from __future__ import annotations

import os
import pty
import re
import select
import shlex
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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
DNS_LINK_MAX_RETRIES = 2
SSH_SEND_RETRIES = 3
SSH_SEND_RETRY_DELAY = 2
APP_RUNTIME_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)


class ChatTransport:
    """Client-side bridge to the remote ``chat.sh`` protocol."""

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
            self.retry_counts: Dict[str, int] = {ip: 0 for ip in self.dns_ips}
            self._pending_restarts: List[str] = []
        else:
            self.status = {"ssh": "unknown"}
            self.fail_counts = {"ssh": 0}
            self.last_online_at = {"ssh": None}
            self.retry_counts = {}
            self._pending_restarts = []
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
            "waiting for network",
            "kex_exchange_identification",
            "connection reset by peer",
            "connection reset",
            "read: connection reset",
            "permission denied",
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

    def _is_removed(self, label: str) -> bool:
        """True if *label* was removed via remove_dns_link."""
        return self.mode == "dns" and label not in self.dns_ips and label != "ssh"

    def _mark_success(self, label: str) -> None:
        if self._is_removed(label):
            return
        self.status[label] = "ok"
        self.fail_counts[label] = 0
        self.retry_counts[label] = 0
        self.last_online_at[label] = datetime.now(timezone.utc)

    def _needs_restart(self, error: str) -> bool:
        return "connection closed by unknown port 65535" in (error or "").lower()

    def _mark_failure(self, label: str, error: str) -> None:
        if self._is_removed(label):
            return
        # Link exhausted its restart retries — permanently failed.
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
        """Return and clear list of DNS IPs that need their slipstream restarted."""
        restarts = self._pending_restarts[:]
        self._pending_restarts.clear()
        return restarts

    def remove_dns_link(self, label: str) -> bool:
        """Remove one DNS link from runtime state (UI-only delete)."""
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
        self.retry_counts.pop(label, None)
        self.last_online_at.pop(label, None)
        return True

    def last_online_age_seconds(self, label: str, now: Optional[datetime] = None) -> Optional[int]:
        when = self.last_online_at.get(label)
        if when is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        return max(0, int((now - when).total_seconds()))

    @staticmethod
    def normalize_news_range(range_spec: str) -> str:
        """Accept '10', '20-10' and '20 10' forms."""
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
        # Snapshot current links to avoid races with remove_dns_link.
        current_links = list(zip(self.dns_ips, self.proxy_ports))
        executor = ThreadPoolExecutor(max_workers=max(1, len(current_links)))
        futures = [
            executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
            for ip, port in current_links
        ]
        for future in as_completed(futures):
            ip, state, output, error = future.result()
            if state == "ok":
                self._mark_success(ip)
                if first_output is None:
                    first_output = output
                # Got a successful read — cancel remaining futures and return early.
                executor.shutdown(wait=False, cancel_futures=True)
                live = set(self.dns_ips)
                return first_output, {k: v for k, v in self.status.items() if k in live}
            else:
                self._mark_failure(ip, error)
            if error and state == "fail":
                self.last_error = f"{ip}: {error}"
        # All failed — return no output.
        live = set(self.dns_ips)
        return first_output, {k: v for k, v in self.status.items() if k in live}

    def send_message(self, name: str, text: str) -> Tuple[bool, str, Dict[str, str]]:
        msg_id = uuid.uuid4().hex
        remote_command = (
            f"bash {self.remote_script} -n "
            f"{shlex.quote(name)} {shlex.quote(msg_id)} {shlex.quote(text)}"
        )

        if self.mode == "ssh":
            last_proc = None
            for attempt in range(SSH_SEND_RETRIES):
                try:
                    last_proc = self._remote_run(
                        remote_command, timeout=REMOTE_COMMAND_TIMEOUT
                    )
                    break
                except Exception as exc:
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
        current_links = list(zip(self.dns_ips, self.proxy_ports))
        executor = ThreadPoolExecutor(max_workers=max(1, len(current_links)))
        futures = [
            executor.submit(self._run_link_command, ip, port, remote_command, REMOTE_COMMAND_TIMEOUT)
            for ip, port in current_links
        ]
        try:
            for future in as_completed(futures):
                ip, state, _output, error = future.result()
                if state == "ok":
                    self._mark_success(ip)
                    successes += 1
                    executor.shutdown(wait=False, cancel_futures=True)
                    live = set(self.dns_ips)
                    return True, "", {k: v for k, v in self.status.items() if k in live}
                self._mark_failure(ip, error)
                if state == "fail":
                    errors.append(f"{ip}: {error or 'send failed'}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if errors:
            self.last_error = "; ".join(errors)
        live = set(self.dns_ips)
        return successes > 0, "; ".join(errors), {k: v for k, v in self.status.items() if k in live}

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
            zip(self.dns_ips, self.proxy_ports),
            key=lambda item: {"ok": 0, "unknown": 1, "fail": 2}.get(self.status.get(item[0], "unknown"), 1),
        )
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

    def touch_presence(self, name: str) -> None:
        remote_command = f"bash {self.remote_script} -u {shlex.quote(name)}"
        if self.mode == "ssh":
            try:
                self._remote_run(remote_command, timeout=REMOTE_COMMAND_TIMEOUT)
            except Exception:
                return
            return
        for ip, port in zip(self.dns_ips, self.proxy_ports):
            try:
                proc = self._remote_run(remote_command, proxy_port=port, timeout=REMOTE_COMMAND_TIMEOUT)
            except Exception:
                continue
            if proc.returncode == 0:
                return

    def fetch_online_users(self, window_seconds: int = 90) -> List[str]:
        remote_command = f"bash {self.remote_script} -w {int(window_seconds)}"
        if self.mode == "ssh":
            try:
                proc = self._remote_run(remote_command, timeout=REMOTE_COMMAND_TIMEOUT)
            except Exception:
                return []
            if proc.returncode != 0:
                return []
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

        ordered_links = sorted(
            zip(self.dns_ips, self.proxy_ports),
            key=lambda item: {"ok": 0, "unknown": 1, "fail": 2}.get(self.status.get(item[0], "unknown"), 1),
        )
        for ip, port in ordered_links:
            try:
                proc = self._remote_run(remote_command, proxy_port=port, timeout=REMOTE_COMMAND_TIMEOUT)
            except Exception:
                continue
            if proc.returncode == 0:
                return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return []

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

