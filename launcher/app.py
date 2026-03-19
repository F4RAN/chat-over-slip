"""Launcher TUI for direct SSH or DNSTT-backed SSH commands."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DirectoryTree, Footer, Header, Input, SelectionList, Static

PROJECT_ROOT = (
    Path(getattr(sys, "_MEIPASS")).resolve()
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
APP_STATE_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chat_tui.app import ChatTransport, ChatView

SLIPSTREAM_START_DELAY = 0.5

class FilePickerScreen(ModalScreen):
    """Modal file browser to select a DNS result file."""
    BINDINGS = [Binding("q", "cancel", "Cancel")]

    def __init__(self, start_path: Optional[Path] = None, **kwargs):
        super().__init__(**kwargs)
        self.start_path = start_path or Path.home()
        self._selected = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Select DNS file (result.txt) - Enter to confirm[/]", id="picker-title")
            yield DirectoryTree(str(self.start_path), id="dir-tree")
            with Horizontal():
                yield Button("Select", variant="primary", id="pick-select")
                yield Button("Cancel", id="pick-cancel")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._selected = Path(event.path)

    def on_mount(self) -> None:
        self.query_one("#dir-tree", DirectoryTree).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pick-select" and self._selected:
            self.dismiss(self._selected)
        elif event.button.id == "pick-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SSHScreen(Static):
    """Form for direct SSH connection."""
    def __init__(self, initial_state: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        self.initial_state = initial_state or {}
        self.host_input = None
        self.user_input = None
        self.pass_input = None
        self.name_input = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Direct SSH[/]")
            yield Input(placeholder="Host (e.g. 65.109.217.21)", id="host")
            yield Input(placeholder="Username", id="user")
            yield Input(placeholder="Password", id="pass", password=True)
            yield Input(placeholder="Display name", id="name")
            yield Input(placeholder="Remote chat.sh path", id="remote-script", value="~/chat-over-dnstt/chat.sh")
            yield Button("Connect", variant="primary", id="connect-btn")

    def on_mount(self):
        self.host_input = self.query_one("#host", Input)
        self.user_input = self.query_one("#user", Input)
        self.pass_input = self.query_one("#pass", Input)
        self.name_input = self.query_one("#name", Input)
        self.remote_script_input = self.query_one("#remote-script", Input)
        self.host_input.value = self.initial_state.get("host", "")
        self.user_input.value = self.initial_state.get("user", "")
        self.name_input.value = self.initial_state.get("name", "")
        self.remote_script_input.value = self.initial_state.get("remote_script", "~/chat-over-dnstt/chat.sh")

    def get_values(self):
        return {
            "host": self.host_input.value.strip(),
            "user": self.user_input.value.strip(),
            "password": self.pass_input.value.strip(),
            "name": self.name_input.value.strip(),
            "remote_script": self.remote_script_input.value.strip() or "~/chat-over-dnstt/chat.sh",
        }


class DNSTTScreen(Static):
    """Form for DNSTT: DNS list, slipstream path, user."""
    def __init__(self, dns_file=None, initial_state: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        self.initial_state = initial_state or {}
        self.dns_file = dns_file or str(Path.home() / "Desktop" / "Projects" / "test-dns" / "result.txt")
        self._loaded_ips = []
        self.scanner_proc: Optional[subprocess.Popen] = None
        self.scanner_output_file = APP_STATE_ROOT / "scanner-result.txt"
        self._scan_timer = None
        self._scan_finished_at: Optional[float] = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]DNSTT (Slipstream)[/]")
            yield Input(placeholder="Slipstream path (e.g. ~/Desktop/slipstream-rust)", id="slip-path")
            yield Input(placeholder="Domain (e.g. t.qtn.at)", id="domain", value="t.qtn.at")
            yield Input(placeholder="SSH Username", id="user")
            yield Input(placeholder="SSH Password", id="pass", password=True)
            yield Input(placeholder="Display name", id="name")
            yield Input(placeholder="Remote chat.sh path", id="remote-script", value="~/chat-over-dnstt/chat.sh")
            with Horizontal():
                yield Input(placeholder="DNS file path (result.txt)", id="dns-file-path")
                yield Button("Browse", id="dns-browse-btn")
            with Horizontal():
                yield Input(placeholder="Scanner input file (optional)", id="scan-input")
                yield Button("Scan", id="scan-btn")
            yield Static("[dim]Select DNS IPs (Space=toggle)[/]:")
            yield SelectionList(id="dns-ip-list")
            yield Static("Not scanned yet", id="scan-status")
            yield Input(placeholder="Or add DNS IP (comma-separated)", id="dns-extra")
            yield Button("Connect", variant="primary", id="connect-dns-btn")

    def on_mount(self):
        self.slip_path = self.query_one("#slip-path", Input)
        self.domain = self.query_one("#domain", Input)
        self.user_input = self.query_one("#user", Input)
        self.pass_input = self.query_one("#pass", Input)
        self.name_input = self.query_one("#name", Input)
        self.remote_script_input = self.query_one("#remote-script", Input)
        self.dns_path_input = self.query_one("#dns-file-path", Input)
        self.scan_input = self.query_one("#scan-input", Input)
        self.scan_status = self.query_one("#scan-status", Static)
        self.dns_list = self.query_one("#dns-ip-list", SelectionList)
        self.dns_extra = self.query_one("#dns-extra", Input)
        self.slip_path.value = self.initial_state.get("slip_path", str(Path.home() / "Desktop" / "slipstream-rust"))
        self.domain.value = self.initial_state.get("domain", "t.qtn.at")
        self.user_input.value = self.initial_state.get("user", "")
        self.name_input.value = self.initial_state.get("name", "")
        self.remote_script_input.value = self.initial_state.get("remote_script", "~/chat-over-dnstt/chat.sh")
        self.dns_path_input.value = self.initial_state.get("dns_file_path", self.dns_file)
        self.scan_input.value = self.initial_state.get("scanner_input_file", "")
        self.dns_extra.value = self.initial_state.get("dns_extra", "")
        self._scan_timer = self.set_interval(2, self._poll_scan_result, pause=True)
        self._load_dns()
    def _load_dns(self):
        path = Path(self.dns_path_input.value.strip()).expanduser()
        self.dns_list.clear_options()
        if hasattr(self.dns_list, "deselect_all"):
            self.dns_list.deselect_all()
        self._loaded_ips = []
        entries = []
        if path.exists():
            for line in path.read_text().splitlines():
                if "IP:" in line and "Time:" in line:
                    parts = line.split("IP:")[1].strip().split("-")
                    ip = parts[0].strip()
                    t = parts[1].replace("Time:", "").strip() if len(parts) > 1 else ""
                    if ip:
                        entries.append((ip, t))
        if self.scanner_output_file.exists():
            for line in self.scanner_output_file.read_text().splitlines():
                if "IP:" in line and "Time:" in line:
                    parts = line.split("IP:")[1].strip().split("-")
                    ip = parts[0].strip()
                    t = parts[1].replace("Time:", "").strip() if len(parts) > 1 else ""
                    if ip:
                        entries.append((ip, t))
        seen = set()
        for ip, t in entries:
            if ip and ip not in seen:
                seen.add(ip)
                self._loaded_ips.append(ip)
                self.dns_list.add_option((f"{ip}  ({t})", ip, True))
        if not self._loaded_ips:
            self.dns_list.add_option(("(no file/scan results - browse, scan, or add IPs below)", ""))

    def _on_file_picked(self, path: Optional[Path]) -> None:
        if path:
            self.dns_path_input.value = str(path)
            # Keep behavior explicit: a newly browsed list replaces previous entries.
            self.dns_extra.value = ""
            self._load_dns()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dns-browse-btn":
            start = Path(self.dns_path_input.value or ".").expanduser()
            if not start.is_dir():
                start = start.parent if start.exists() else Path.home()
            picker = FilePickerScreen(start_path=start)
            result = await self.app.push_screen_wait(picker)
            self._on_file_picked(result)
        elif event.button.id == "scan-btn":
            self._start_scan()

    def _start_scan(self) -> None:
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self.scan_status.update("[yellow]Scanning...[/]")
            return
        scanner_script = PROJECT_ROOT / "scanner.py"
        input_file = Path(self.scan_input.value.strip()).expanduser()
        if not input_file.exists():
            self.notify("Scanner input file not found", severity="error")
            return
        if not scanner_script.exists():
            self.notify("scanner.py not found", severity="error")
            return
        self.scanner_output_file.parent.mkdir(parents=True, exist_ok=True)
        self.scanner_output_file.write_text("")
        cmd = [
            sys.executable,
            str(scanner_script),
            "-f",
            str(input_file),
            "-o",
            str(self.scanner_output_file),
        ]
        self.scanner_proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._scan_finished_at = None
        self.scan_status.update("[yellow]Scanning...[/]")
        if self._scan_timer:
            self._scan_timer.resume()

    def _poll_scan_result(self) -> None:
        if self.scanner_output_file.exists():
            self._load_dns()
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self.scan_status.update("[yellow]Scanning...[/]")
            return
        if self.scanner_proc:
            if self._scan_finished_at is None:
                self._scan_finished_at = time.time()
            elapsed = max(0, int(time.time() - self._scan_finished_at))
            if elapsed < 60:
                age = "just now"
            elif elapsed < 3600:
                age = f"{elapsed // 60}m ago"
            elif elapsed < 86400:
                age = f"{elapsed // 3600}h ago"
            else:
                age = f"{elapsed // 86400}d ago"
            self.scan_status.update(f"[green]Scanned:[/] {age}")
        else:
            self.scan_status.update("[dim]Not scanned yet[/]")
            if self._scan_timer:
                self._scan_timer.pause()

    def get_selected_ips(self):
        ips = list(self.dns_list.selected)
        if not ips:
            ips = list(self._loaded_ips)
        extra = self.dns_extra.value.strip()
        if extra:
            for ip in extra.replace(",", " ").split():
                ip = ip.strip()
                if ip and ip not in ips:
                    ips.append(ip)
        return ips

    def get_values(self):
        return {
            "slip_path": Path(self.slip_path.value.strip()).expanduser(),
            "domain": self.domain.value.strip() or "t.qtn.at",
            "user": self.user_input.value.strip(),
            "password": self.pass_input.value.strip(),
            "name": self.name_input.value.strip(),
            "remote_script": self.remote_script_input.value.strip() or "~/chat-over-dnstt/chat.sh",
            "dns_file_path": self.dns_path_input.value.strip(),
            "scanner_input_file": self.scan_input.value.strip(),
            "dns_extra": self.dns_extra.value.strip(),
            "ips": self.get_selected_ips(),
        }


class ChatSessionScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, config: dict, startup_lines: Optional[list[str]] = None):
        super().__init__()
        self.config = config
        self.startup_lines = startup_lines or []
        self.slip_procs = []
        self.chat_view: Optional[ChatView] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        transport = ChatTransport(
            mode=self.config["mode"],
            host=self.config.get("host", ""),
            domain=self.config.get("domain", ""),
            ssh_user=self.config["user"],
            ssh_pass=self.config["password"],
            remote_script=self.config["remote_script"],
            proxy_ports=self.config.get("proxy_ports", []),
            dns_ips=self.config.get("dns_ips", []),
        )
        self.chat_view = ChatView(transport, self.config.get("name") or self.config["user"], self.startup_lines)
        yield self.chat_view
        yield Footer()

    def on_mount(self) -> None:
        if self.config["mode"] == "dns":
            asyncio.create_task(self._start_dns_clients())

    async def _start_dns_clients(self) -> None:
        slip_path = Path(self.config["slip_path"])
        domain = self.config["domain"]
        for ip, port in zip(self.config["dns_ips"], self.config["proxy_ports"]):
            if self.chat_view:
                self.chat_view.write_system(f"Starting slipstream: {ip} -> 127.0.0.1:{port}")
            proc = subprocess.Popen(
                [
                    "/usr/local/bin/slipstream-client",
                    "--tcp-listen-port",
                    str(port),
                    "--resolver",
                    f"{ip}:53",
                    "--domain",
                    domain,
                ],
                cwd=str(slip_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.slip_procs.append(proc)
            await asyncio.sleep(SLIPSTREAM_START_DELAY)
        if self.chat_view:
            self.chat_view.write_system("Slipstream clients started. Waiting for links...")
            await self.chat_view.refresh_now()

    def _stop_slip_clients(self) -> None:
        for proc in self.slip_procs:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.slip_procs = []

    def action_back(self) -> None:
        self._stop_slip_clients()
        if self.chat_view:
            self.chat_view.shutdown()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self._stop_slip_clients()
        if self.chat_view:
            self.chat_view.shutdown()


class LauncherApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #tabs {
        height: auto;
        layout: horizontal;
    }
    #tabs Button {
        margin: 1 2;
    }
    #content {
        height: 1fr;
        padding: 2;
        border: solid $primary;
    }
    SSHScreen, DNSTTScreen {
        padding: 1;
        width: 100%;
    }
    SelectionList {
        height: 10;
    }
    #dir-tree {
        height: 15;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, dns_file=None, initial_state: Optional[dict] = None, state_path: Optional[Path] = None):
        super().__init__()
        self.mode = "ssh"
        self.dns_file = dns_file
        self.initial_state = initial_state or {}
        self.state_path = state_path

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="tabs"):
            yield Button("SSH", id="tab-ssh")
            yield Button("DNSTT", id="tab-dns")
        with Container(id="content"):
            self.ssh_screen = SSHScreen(initial_state=self.initial_state.get("ssh", {}))
            self.dns_screen = DNSTTScreen(dns_file=self.dns_file, initial_state=self.initial_state.get("dns", {}))
            yield self.ssh_screen
            yield self.dns_screen
        yield Footer()

    def on_mount(self):
        self.dns_screen.display = False

    def _show_ssh(self):
        self.ssh_screen.display = True
        self.dns_screen.display = False
        self.mode = "ssh"

    def _show_dns(self):
        self.ssh_screen.display = False
        self.dns_screen.display = True
        self.mode = "dns"

    def on_button_pressed(self, e: Button.Pressed):
        if e.button.id == "tab-ssh":
            self._show_ssh()
        elif e.button.id == "tab-dns":
            self._show_dns()
        elif e.button.id == "connect-btn":
            self._do_ssh_connect()
        elif e.button.id == "connect-dns-btn":
            self._do_dns_connect()

    def _do_ssh_connect(self):
        v = self.ssh_screen.get_values()
        if not v["host"] or not v["user"]:
            self.notify("Host and user required", severity="error")
            return
        if self.state_path:
            save_launcher_state(self.state_path, v)
        self.push_screen(
            ChatSessionScreen(
                {
                    "mode": "ssh",
                    "host": v["host"],
                    "user": v["user"],
                    "password": v["password"],
                    "name": v.get("name"),
                    "remote_script": v["remote_script"],
                }
            )
        )

    def _do_dns_connect(self):
        v = self.dns_screen.get_values()
        if not v["user"]:
            self.notify("Username required", severity="error")
            return
        if not v["ips"]:
            self.notify("Select at least one DNS IP", severity="error")
            return
        if not v["slip_path"] or not v["slip_path"].exists():
            self.notify("Slipstream path must exist", severity="error")
            return
        if self.state_path:
            save_launcher_state(self.state_path, v)
        base_port = 8000
        self.push_screen(
            ChatSessionScreen(
                {
                    "mode": "dns",
                    "slip_path": str(v["slip_path"]),
                    "domain": v["domain"],
                    "user": v["user"],
                    "password": v["password"],
                    "name": v.get("name"),
                    "remote_script": v["remote_script"],
                    "dns_ips": v["ips"],
                    "proxy_ports": [base_port + index for index in range(len(v["ips"]))],
                },
                startup_lines=[
                    "Preparing DNSTT chat session...",
                    f"Using {len(v['ips'])} DNS link(s).",
                ],
            )
        )

    def action_quit(self):
        self.exit()


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
            "remote_script": result.get("remote_script", "~/chat-over-dnstt/chat.sh"),
        }
    elif "ips" in result:
        state["dns"] = {
            "slip_path": str(result.get("slip_path", "")),
            "domain": result.get("domain", "t.qtn.at"),
            "user": result.get("user", ""),
            "name": result.get("name", ""),
            "remote_script": result.get("remote_script", "~/chat-over-dnstt/chat.sh"),
            "dns_file_path": result.get("dns_file_path", ""),
            "scanner_input_file": result.get("scanner_input_file", ""),
            "dns_extra": result.get("dns_extra", ""),
        }
    state_path.write_text(json.dumps(state, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dns-file", default="", help="Path to DNS result file")
    args = ap.parse_args()
    dns_file = args.dns_file or str(Path(__file__).parent.parent / ".." / "test-dns" / "result.txt")
    if not Path(dns_file).exists():
        dns_file = None
    state_path = APP_STATE_ROOT / ".launcher_state.json"
    initial_state = load_launcher_state(state_path)

    app = LauncherApp(dns_file=dns_file, initial_state=initial_state, state_path=state_path)
    app.run()


if __name__ == "__main__":
    main()
