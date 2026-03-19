"""PySide6 desktop UI for chat-over-dnstt."""

from __future__ import annotations

import argparse
import faulthandler
import html
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

PROJECT_ROOT = (
    Path(getattr(sys, "_MEIPASS")).resolve()
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from chat_common.session import (
    ChatSessionModel,
    DEFAULT_DOMAIN,
    DEFAULT_REMOTE_SCRIPT,
    NotificationPlayer,
    SlipstreamManager,
    as_upload_command,
    has_rtl,
    delete_secure_password,
    load_launcher_state,
    load_secure_password,
    parse_dns_result_file,
    resolve_download_target,
    save_launcher_state,
    save_secure_password,
)
from chat_common.transport import ChatTransport, STATUS_POLL_INTERVAL

CRASH_LOG_PATH = Path.home() / ".chat-over-dnstt" / "crash.log"


log = logging.getLogger("chat_gui")


def _setup_crash_logging() -> None:
    """Enable faulthandler (SIGSEGV/SIGABRT tracebacks), excepthook and rich file logging."""
    log_dir = CRASH_LOG_PATH.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # ---- rich file logger (always-on, for diagnosing non-fatal issues) ----
    _log_file = log_dir / "gui.log"
    file_handler = logging.handlers.RotatingFileHandler(
        _log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(file_handler)
    log.setLevel(logging.DEBUG)
    log.info("=== App start (PID %d) ===", os.getpid())

    try:
        f = open(CRASH_LOG_PATH, "a", encoding="utf-8")
        f.write(
            f"\n--- {datetime.now().isoformat()} --- App start ---\n"
            "Note: Segfaults during send often occur in subprocess.run (worker thread).\n"
        )
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        _orig_excepthook = sys.excepthook

        def _excepthook(etype, value, tb):
            log.critical("Uncaught exception", exc_info=(etype, value, tb))
            f.write(f"\n--- {datetime.now().isoformat()} --- Uncaught exception ---\n")
            traceback.print_exception(etype, value, tb, file=f)
            f.flush()
            _orig_excepthook(etype, value, tb)

        sys.excepthook = _excepthook
    except Exception:
        faulthandler.enable(all_threads=True)

APP_STATE_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else PROJECT_ROOT
)
INCOMING_SOUND_PATH = PROJECT_ROOT / "assets" / "sounds" / "incoming-message.mp3"
OUTGOING_SOUND_PATH = PROJECT_ROOT / "assets" / "sounds" / "outgoing-send.wav"
INCOMING_SOUND_DEBOUNCE_SECONDS = 1.2
ONLINE_REFRESH_INTERVAL_MS = 5 * 60 * 1000
ONLINE_PRESENCE_WINDOW_SECONDS = 15 * 60
ONLINE_FIRST_REFRESH_DELAY_MS = 10 * 1000
SCANNER_POLL_INTERVAL_MS = 2000


class WorkerSignals(QObject):
    finished = Signal(object, object)


class FunctionWorker(QRunnable):
    def __init__(self, fn: Callable, label: str = ""):
        super().__init__()
        self.fn = fn
        self.label = label or getattr(fn, "__name__", "?")
        self.signals = WorkerSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            result = self.fn()
            if not self._cancelled:
                self.signals.finished.emit(result, None)
        except Exception as exc:
            log.exception("Worker [%s] crashed", self.label)
            if not self._cancelled:
                self.signals.finished.emit(None, exc)


def _configure_fonts(app: QApplication) -> None:
    font_candidates = [
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn-Regular.ttf",
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn-Medium.ttf",
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn-SemiBold.ttf",
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn-Bold.ttf",
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn-Black.ttf",
        PROJECT_ROOT / "assets" / "fonts" / "Vazirmatn[wght].ttf",
    ]
    selected_family = ""
    for candidate in font_candidates:
        if not candidate.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(candidate))
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            selected_family = families[0]
            break
    if selected_family:
        font = QFont(selected_family, 12)
        font.setWeight(QFont.Normal)
        app.setFont(font)


def _app_icon_path() -> Optional[Path]:
    candidates = [
        PROJECT_ROOT / "assets" / "vitalize-app-icon-256.png",
        PROJECT_ROOT / "assets" / "vitalize-app-icon-64.png",
        PROJECT_ROOT / "assets" / "vitalize-app-icon.ico",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


class EmojiPickerDialog(QDialog):
    def __init__(self, parent: QWidget, on_pick: Callable[[str], None]):
        super().__init__(parent)
        self.on_pick = on_pick
        self.setWindowFlags(Qt.Popup)
        self.setObjectName("EmojiPicker")
        self.setWindowTitle("Emoji")
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(6)
        emojis = [
            "😀", "😁", "😂", "🤣", "😊", "😍", "😘", "😎", "🤔", "🙄",
            "😴", "🤯", "😭", "😡", "👍", "👎", "👏", "🙏", "❤️", "🔥",
            "🎉", "💯", "✅", "❌", "🌹", "🌟", "🤝", "👀", "😅", "😇",
        ]
        cols = 6
        for idx, emoji in enumerate(emojis):
            button = QPushButton(emoji)
            button.setFixedSize(42, 38)
            button.clicked.connect(lambda _checked=False, e=emoji: self._pick(e))
            layout.addWidget(button, idx // cols, idx % cols)
        self.setStyleSheet(
            """
            QDialog#EmojiPicker { background: #0f172a; border: 1px solid #334155; border-radius: 10px; }
            QDialog#EmojiPicker QPushButton {
                font-size: 20px;
                font-weight: 400;
                padding: 2px 0 3px 0;
                border: 1px solid #334155;
                background: #111827;
                border-radius: 8px;
            }
            QDialog#EmojiPicker QPushButton:hover { background: #1f2937; border-color: #475569; }
            """
        )

    def _pick(self, emoji: str) -> None:
        self.on_pick(emoji)
        self.close()


class MessageBubble(QFrame):
    def __init__(
        self,
        timestamp: str,
        user: str,
        text: str,
        own: bool,
        pending: bool,
        rtl: bool,
        system: bool,
        link_handler: Optional[Callable[[str], None]] = None,
        body_is_html: bool = False,
    ):
        super().__init__()
        self.setObjectName("MessageBubble")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 6)
        layout.setSpacing(2)
        self.setMaximumWidth(560)
        self.setMinimumWidth(72)

        direction = "rtl" if rtl else "ltr"
        align = "right" if rtl else "left"

        if system:
            self.setProperty("kind", "system")
            body_html = self._wrap_html(text, direction, align, body_is_html)
            body = QLabel(body_html)
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            body.setTextInteractionFlags(Qt.TextBrowserInteraction)
            body.setOpenExternalLinks(False)
            body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            if link_handler:
                body.linkActivated.connect(link_handler)
            layout.addWidget(body)
            self._body_label = body
            self._body_html = body_html
            self._meta_html = ""
            return

        self.setProperty("kind", "own" if own else "other")
        display = text
        body_html = self._wrap_html(display, direction, align, body_is_html)
        body = QLabel(body_html)
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        body.setOpenExternalLinks(False)
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        if link_handler:
            body.linkActivated.connect(link_handler)
        layout.addWidget(body)
        meta_parts = []
        if user:
            meta_parts.append(html.escape(user))
        if timestamp:
            meta_parts.append(html.escape(timestamp))
        if pending:
            meta_parts.append("\u23f3")
        meta_text = " &middot; ".join(meta_parts)
        meta = QLabel(f'<div style="text-align:right">{meta_text}</div>')
        meta.setTextFormat(Qt.RichText)
        meta.setObjectName("BubbleMeta")
        meta.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(meta)
        self._body_label = body
        self._meta_label = meta
        self._body_html = body_html
        self._meta_html = f'<div style="text-align:right">{meta_text}</div>'

    def computed_height(self, available_width: int) -> int:
        bubble_w = min(max(available_width - 48, 120), 560)
        inner_w = max(bubble_w - 20, 96)

        body_h = 20
        if hasattr(self, "_body_label"):
            body_h = self._body_label.heightForWidth(inner_w)
            if body_h <= 0:
                body_h = self._body_label.sizeHint().height()

        meta_h = 0
        if hasattr(self, "_meta_label"):
            meta_h = self._meta_label.heightForWidth(inner_w)
            if meta_h <= 0:
                meta_h = self._meta_label.sizeHint().height()

        return max(body_h + meta_h + 18, 32)

    @staticmethod
    def _wrap_html(text: str, direction: str, align: str, already_html: bool) -> str:
        inner = text if already_html else html.escape(text)
        return f'<div dir="{direction}" style="text-align:{align}">{inner}</div>'


class MessageRow(QWidget):
    def __init__(
        self,
        bubble: MessageBubble,
        align_right: bool,
        message=None,
        on_delete: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self._bubble = bubble
        self._message = message
        self._on_delete = on_delete
        self.setLayoutDirection(Qt.RightToLeft if align_right else Qt.LeftToRight)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)
        layout.addWidget(bubble, 0, Qt.AlignTop)
        layout.addStretch(1)
        if on_delete is not None:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        ctx = QMenu(self)
        action = ctx.addAction("Delete")
        action.triggered.connect(self._on_delete)
        ctx.exec(self.mapToGlobal(pos))

    def computed_height(self, available_width: int) -> int:
        return self._bubble.computed_height(available_width) + 10


class LinkStateRow(QWidget):
    def __init__(self, label: str, state: str, age_text: str = "", on_remove: Optional[Callable[[], None]] = None):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(10, 10)
        color = "#9ca3af"
        if state == "ok":
            color = "#22c55e"
        elif state == "fail":
            color = "#ef4444"
        dot.setStyleSheet(f"border-radius: 5px; background: {color};")
        text = QLabel(f"{label}")
        text_state = QLabel(age_text or state)
        text_state.setStyleSheet("color: #9ca3af;")
        layout.addWidget(dot)
        layout.addWidget(text)
        layout.addStretch(1)
        layout.addWidget(text_state)
        if on_remove is not None and state == "fail":
            remove_btn = QPushButton("x")
            remove_btn.setFixedSize(22, 22)
            remove_btn.setStyleSheet(
                "QPushButton { color: #ef4444; border: 1px solid #7f1d1d; border-radius: 11px; "
                "background: #1f1010; font-weight: 700; padding: 0px; }"
                "QPushButton:hover { background: #2f1212; border-color: #ef4444; }"
            )
            remove_btn.clicked.connect(on_remove)
            layout.addWidget(remove_btn)


class DateSeparatorRow(QWidget):
    def __init__(self, text: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        left_line = QFrame()
        left_line.setFrameShape(QFrame.HLine)
        left_line.setFrameShadow(QFrame.Plain)
        left_line.setStyleSheet("color: #334155; background: #334155; min-height: 1px; max-height: 1px;")
        right_line = QFrame()
        right_line.setFrameShape(QFrame.HLine)
        right_line.setFrameShadow(QFrame.Plain)
        right_line.setStyleSheet("color: #334155; background: #334155; min-height: 1px; max-height: 1px;")
        label = QLabel(text)
        label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(left_line, 1)
        layout.addWidget(label)
        layout.addWidget(right_line, 1)


class ChatWindow(QMainWindow):
    progress_signal = Signal(str, object, str)
    system_signal = Signal(str)

    def __init__(self, config: dict, startup_lines: Optional[List[str]] = None, on_close_callback: Optional[Callable] = None):
        super().__init__()
        self.config = config
        self.on_close_callback = on_close_callback
        self.startup_lines = startup_lines or []
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self.thread_pool.setExpiryTimeout(-1)
        self.transport = ChatTransport(
            mode=config["mode"],
            host=config.get("host", ""),
            domain=config.get("domain", ""),
            ssh_user=config["user"],
            ssh_pass=config["password"],
            remote_script=config.get("remote_script", DEFAULT_REMOTE_SCRIPT),
            proxy_ports=config.get("proxy_ports", []),
            dns_ips=config.get("dns_ips", []),
        )
        self.session = ChatSessionModel(display_name=config.get("name") or config["user"])
        self.slipstream_manager: Optional[SlipstreamManager] = None
        self._transport_busy = False
        self._active_transport_kind = ""
        self._transport_queue: List[Tuple[str, Callable, Callable]] = []
        self.read_busy = False
        self.retry_busy = False
        self._rendering = False
        self.online_busy = False
        self._online_refresh_started = False
        self.server_online_users: List[str] = []
        self.emoji_picker: Optional[EmojiPickerDialog] = None
        self._last_incoming_sound_at = 0.0
        self._is_shutting_down = False
        self._message_filter_mode = "all"
        self._last_render_messages = []
        self._hidden_messages: set = set()
        self._active_worker: Optional[FunctionWorker] = None
        self.setWindowTitle(f"Chat over DNSTT - {self.session.display_name}")
        self.resize(1150, 760)
        self.setAcceptDrops(True)
        icon_path = _app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        self._setup_ui()
        self._setup_actions()
        self.progress_signal.connect(self._on_progress_update)
        self.system_signal.connect(self._append_system_message)
        for line in self.startup_lines:
            self._append_system_message(line)
        self._render_status(self.transport.status, self.transport.last_error)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(int(STATUS_POLL_INTERVAL * 1000))
        self.online_timer = QTimer(self)
        self.online_timer.timeout.connect(self._refresh_online_users)
        self._poll()
        if config["mode"] == "dns":
            self._start_slipstream_clients()

    def _play_outgoing_sound(self) -> None:
        if self._is_shutting_down:
            return
        NotificationPlayer.play(str(OUTGOING_SOUND_PATH))

    def _play_incoming_sound(self) -> None:
        if self._is_shutting_down:
            return
        now = time.monotonic()
        if now - self._last_incoming_sound_at < INCOMING_SOUND_DEBOUNCE_SECONDS:
            return
        self._last_incoming_sound_at = now
        NotificationPlayer.play(str(INCOMING_SOUND_PATH))

    def _setup_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        title = QLabel("Chat Room")
        title.setObjectName("ChatTitle")
        subtitle = QLabel(f"Mode: {self.transport.mode.upper()}  User: {self.session.display_name}")
        subtitle.setObjectName("ChatSubtitle")
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_container = QWidget()
        self.chat_container.setObjectName("ChatContainer")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_layout.setSpacing(2)
        self.chat_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_container)
        filter_row = QHBoxLayout()
        self.filter_all_btn = QPushButton("All")
        self.filter_news_btn = QPushButton("News")
        self.filter_messages_btn = QPushButton("Messages")
        for button in (self.filter_all_btn, self.filter_news_btn, self.filter_messages_btn):
            button.setCheckable(True)
            button.setProperty("filterButton", True)
            filter_row.addWidget(button)
        self.filter_all_btn.setChecked(True)
        self.filter_all_btn.clicked.connect(lambda: self._set_message_filter("all"))
        self.filter_news_btn.clicked.connect(lambda: self._set_message_filter("news"))
        self.filter_messages_btn.clicked.connect(lambda: self._set_message_filter("messages"))
        filter_row.addStretch(1)
        left_layout.addLayout(filter_row)
        left_layout.addWidget(self.chat_scroll, 1)

        transfer_row = QHBoxLayout()
        self.transfer_label = QLabel("")
        self.transfer_label.setObjectName("TransferStatus")
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setRange(0, 100)
        self.transfer_bar.setValue(0)
        self.transfer_bar.setTextVisible(True)
        self.transfer_bar.hide()
        transfer_row.addWidget(self.transfer_label, 1)
        transfer_row.addWidget(self.transfer_bar)
        left_layout.addLayout(transfer_row)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("Type message. Commands: /clear /upload /download /news")
        self.input_line.setMinimumHeight(44)
        self.input_line.returnPressed.connect(self._send_input)
        self.emoji_btn = QPushButton("😊")
        self.emoji_btn.setToolTip("Emoji")
        self.emoji_btn.setFixedSize(48, 44)
        self.emoji_btn.setStyleSheet("font-size: 22px; padding: 0px;")
        self.emoji_btn.clicked.connect(self._show_emoji_picker)
        attach_btn = QPushButton("Attach")
        attach_btn.setMinimumHeight(44)
        attach_btn.clicked.connect(self._attach_file)
        send_btn = QPushButton("Send")
        send_btn.setMinimumHeight(44)
        send_btn.clicked.connect(self._send_input)
        input_row.addWidget(self.input_line, 1)
        input_row.addWidget(self.emoji_btn)
        input_row.addWidget(attach_btn)
        input_row.addWidget(send_btn)
        left_layout.addLayout(input_row)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right.setMaximumWidth(300)

        status_group = QGroupBox("Connection")
        status_group_layout = QVBoxLayout(status_group)
        self.overall_state_label = QLabel("waiting")
        self.overall_state_label.setObjectName("OverallState")
        self.links_list = QListWidget()
        self.links_list.setObjectName("LinksList")
        self.links_list.setMaximumHeight(150)
        self.last_error_label = QLabel("")
        self.last_error_label.setWordWrap(True)
        self.last_error_label.setObjectName("LastError")
        status_group_layout.addWidget(self.overall_state_label)
        status_group_layout.addWidget(self.links_list)
        status_group_layout.addWidget(self.last_error_label)

        online_group = QGroupBox("Online")
        online_group_layout = QVBoxLayout(online_group)
        self.online_list = QListWidget()
        self.online_list.setObjectName("OnlineList")
        self.online_list.setMaximumHeight(180)
        online_group_layout.addWidget(self.online_list)

        right_layout.addWidget(status_group)
        right_layout.addWidget(online_group)
        right_layout.addStretch(1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        copyright_label = QLabel("Copyright by Vitalize 2026")
        copyright_label.setObjectName("Copyright")
        copyright_label.setAlignment(Qt.AlignCenter)
        root_layout.addWidget(copyright_label)

        self.setStyleSheet(
            """
            QMainWindow { background: #05060a; color: #e5e7eb; }
            QLabel#ChatTitle { font-size: 22px; font-weight: 700; color: #f3f4f6; }
            QLabel#ChatSubtitle { color: #9ca3af; margin-bottom: 4px; }
            QScrollArea#ChatScroll { border: 1px solid #374151; border-radius: 12px; background: #0a1323; }
            QWidget#ChatContainer { background: #0a1323; }
            QGroupBox { border: 1px solid #374151; border-radius: 12px; margin-top: 10px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #dbeafe; }
            QListWidget#LinksList, QListWidget#OnlineList { border: 1px solid #2b3445; border-radius: 8px; background: #0a1323; }
            QLineEdit { border: 1px solid #334155; border-radius: 10px; padding: 9px; background: #0b1220; color: #f9fafb; }
            QPushButton { border: 1px solid #4b5563; border-radius: 10px; padding: 8px 12px; background: #1f2937; color: #f9fafb; }
            QPushButton:hover { background: #2d3b52; border-color: #38bdf8; }
            QLabel#TransferStatus { color: #93c5fd; min-height: 22px; }
            QProgressBar { min-width: 160px; max-width: 220px; border: 1px solid #374151; border-radius: 7px; background: #0b1220; color: #e5e7eb; }
            QProgressBar::chunk { background: #38bdf8; border-radius: 6px; }
            QPushButton[filterButton="true"] {
                padding: 5px 10px;
                min-width: 72px;
                border-radius: 9px;
                background: #111827;
            }
            QPushButton[filterButton="true"]:checked {
                background: #0ea5e9;
                border-color: #38bdf8;
                color: #ffffff;
            }
            QFrame#MessageBubble[kind="own"] { background: #1d8cf8; color: #ffffff; border-radius: 14px; }
            QFrame#MessageBubble[kind="other"] { background: #1f2937; color: #f3f4f6; border-radius: 14px; }
            QFrame#MessageBubble[kind="system"] { background: #27303d; color: #d1d5db; border-radius: 12px; }
            QLabel#BubbleMeta { color: #94a3b8; font-size: 11px; font-weight: 400; }
            QLabel#OverallState { font-weight: 600; color: #bfdbfe; }
            QLabel#LastError { color: #fca5a5; min-height: 26px; }
            QLabel { color: #e5e7eb; font-size: 13px; font-weight: 400; }
            a { color: #7dd3fc; text-decoration: underline; }
            QLabel#Copyright { color: #64748b; font-size: 11px; margin-top: 2px; }
            """
        )

    def _setup_actions(self) -> None:
        menu = self.menuBar().addMenu("Chat")
        refresh_action = QAction("Refresh Now", self)
        refresh_action.triggered.connect(self._poll)
        menu.addAction(refresh_action)

    def _has_transport_task(self, kind: str) -> bool:
        return self._active_transport_kind == kind or any(task_kind == kind for task_kind, _fn, _callback in self._transport_queue)

    def _enqueue_transport_task(
        self,
        kind: str,
        fn: Callable,
        callback: Callable[[object, object], None],
        *,
        dedupe: bool = False,
    ) -> bool:
        if self._is_shutting_down:
            return False
        if dedupe and self._has_transport_task(kind):
            return False
        self._transport_queue.append((kind, fn, callback))
        self._pump_transport_queue()
        return True

    def _pump_transport_queue(self) -> None:
        if self._is_shutting_down or self._transport_busy or not self._transport_queue:
            return
        kind, fn, callback = self._transport_queue.pop(0)
        self._transport_busy = True
        self._active_transport_kind = kind
        log.debug("Transport task starting: %s", kind)
        worker = FunctionWorker(fn, label=kind)
        self._active_worker = worker

        def _finished(result, error) -> None:
            self._active_worker = None
            self._transport_busy = False
            self._active_transport_kind = ""
            if self._is_shutting_down:
                return
            try:
                callback(result, error)
            except Exception:
                log.exception("Crash in transport callback [%s]", kind)
                try:
                    self._append_system_message(f"Internal error in {kind} – see gui.log")
                except Exception:
                    pass
            self._pump_transport_queue()

        worker.signals.finished.connect(_finished)
        self.thread_pool.start(worker)

    def _render_status(self, statuses: Dict[str, str], last_error: str) -> None:
        values = list(statuses.values())
        ok_count = sum(1 for s in values if s == "ok")
        total_count = len(values)
        if ok_count > 0:
            overall = "online"
        elif any(state == "unknown" for state in values):
            overall = "waiting"
        else:
            overall = "offline"
        count_text = f"({ok_count}/{total_count})" if total_count > 1 else ""
        self.overall_state_label.setText(f"{self.transport.mode.upper()}  {overall}  {count_text}".strip())
        self.links_list.clear()
        sort_order = {"ok": 0, "unknown": 1, "fail": 2}
        sorted_links = sorted(statuses.items(), key=lambda kv: sort_order.get(kv[1], 1))
        for label, state in sorted_links:
            row_widget = LinkStateRow(
                label,
                state,
                age_text=self._format_link_age(label, state),
                on_remove=(lambda ip=label: self._remove_dns_link(ip)) if self.transport.mode == "dns" else None,
            )
            item = QListWidgetItem()
            item.setSizeHint(row_widget.sizeHint())
            self.links_list.addItem(item)
            self.links_list.setItemWidget(item, row_widget)
        self.last_error_label.setText(last_error or "")

    @staticmethod
    def _format_age_text(seconds: Optional[int]) -> str:
        if seconds is None:
            return "never"
        if seconds < 60:
            return "now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def _format_link_age(self, label: str, state: str) -> str:
        age = self.transport.last_online_age_seconds(label)
        if state == "unknown" and age is None:
            return "checking..."
        return self._format_age_text(age)

    def _remove_dns_link(self, ip: str) -> None:
        if self.transport.mode != "dns":
            return
        state = self.transport.status.get(ip, "unknown")
        if state != "fail":
            self._append_system_message("Only offline DNS links can be removed")
            return
        old_index = self.transport.dns_ips.index(ip) if ip in self.transport.dns_ips else -1
        if self.slipstream_manager and 0 <= old_index < len(self.slipstream_manager.processes):
            proc = self.slipstream_manager.processes.pop(old_index)
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not self.transport.remove_dns_link(ip):
            return
        # Keep config in sync for later operations.
        self.config["dns_ips"] = list(self.transport.dns_ips)
        self.config["proxy_ports"] = list(self.transport.proxy_ports)
        self._append_system_message(f"Removed offline DNS link: {ip}")
        self._render_status(self.transport.status, self.transport.last_error)

    def _render_online_users(self) -> None:
        self.online_list.clear()
        users = self.server_online_users or self.session.online_users()
        if not users:
            self.online_list.addItem("(no active users in recent messages)")
            return
        for user in users:
            self.online_list.addItem(f"●  {user}")

    def _scroll_to_bottom(self) -> None:
        def _do_scroll() -> None:
            bar = self.chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        QTimer.singleShot(50, _do_scroll)

    def _clear_chat_widgets(self) -> None:
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_chat_widget(self, widget: QWidget) -> None:
        insert_at = max(0, self.chat_layout.count() - 1)
        self.chat_layout.insertWidget(insert_at, widget)

    def _append_system_message(self, text: str, is_html: bool = False) -> None:
        bubble = MessageBubble(
            "",
            "",
            text,
            own=False,
            pending=False,
            rtl=has_rtl(text),
            system=True,
            link_handler=self._on_message_link if is_html else None,
            body_is_html=is_html,
        )
        entry = MessageRow(bubble, align_right=False)
        self._add_chat_widget(entry)
        self._scroll_to_bottom()

    def _set_message_filter(self, mode: str) -> None:
        self._message_filter_mode = mode
        self.filter_all_btn.setChecked(mode == "all")
        self.filter_news_btn.setChecked(mode == "news")
        self.filter_messages_btn.setChecked(mode == "messages")
        self._render_messages(self._last_render_messages)

    def _message_hide_key(self, message) -> Tuple:
        if message.msg_id:
            return ("id", message.msg_id)
        return ("fp", message.timestamp, message.user, message.display_text)

    def _filtered_messages(self, messages) -> List:
        if self._message_filter_mode == "all":
            return list(messages)
        if self._message_filter_mode == "news":
            return [
                message
                for message in messages
                if not message.system and message.user.startswith("news/")
            ]
        return [
            message
            for message in messages
            if not message.system and not message.user.startswith("news/")
        ]

    def _render_messages(self, messages) -> None:
        if self._is_shutting_down or self._rendering:
            return
        self._rendering = True
        self._last_render_messages = list(messages)
        messages = self._filtered_messages(messages)
        messages = [m for m in messages if self._message_hide_key(m) not in self._hidden_messages]
        self._clear_chat_widgets()
        last_date: Optional[str] = None
        for message in messages:
            if message.system:
                bubble = MessageBubble("", "", message.display_text, own=False, pending=False, rtl=message.rtl, system=True)
            else:
                parsed = self._parse_server_time(message.timestamp)
                if parsed:
                    day_key = parsed.strftime("%Y-%m-%d")
                    if day_key != last_date:
                        last_date = day_key
                        self._append_date_separator(self._friendly_day_label(parsed))
                meta_time = self._format_message_time(parsed, fallback=message.timestamp)
                meta_text = f"{meta_time}  {message.user}"
                text = message.display_text
                body_is_html = False
                if message.file_entry:
                    file_name, _relative = message.file_entry
                    safe_name = html.escape(file_name, quote=True)
                    encoded = quote(file_name, safe="")
                    text = f'📎 {safe_name}  <a href="download:{encoded}">⬇</a>'
                    body_is_html = True
                bubble = MessageBubble(
                    meta_text,
                    "",
                    text,
                    own=message.own,
                    pending=message.pending,
                    rtl=message.rtl,
                    system=False,
                    link_handler=self._on_message_link,
                    body_is_html=body_is_html,
                )
            row_ref: List[Optional[QWidget]] = [None]

            def make_on_delete():
                def do_delete():
                    self._on_delete_message(message, row_ref[0])
                return do_delete

            row = MessageRow(
                bubble,
                align_right=(message.own or message.rtl) and not message.system,
                message=message,
                on_delete=make_on_delete(),
            )
            row_ref[0] = row
            self._add_chat_widget(row)
        self._rendering = False
        self._scroll_to_bottom()

    def _append_date_separator(self, text: str) -> None:
        row = DateSeparatorRow(text)
        self._add_chat_widget(row)

    def _parse_server_time(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        # Store/display in local timezone for friendlier chat timestamps.
        return naive.replace(tzinfo=timezone.utc).astimezone()

    def _friendly_day_label(self, when: datetime) -> str:
        today = datetime.now().astimezone().date()
        day = when.date()
        if day == today:
            return "Today"
        if day == today - timedelta(days=1):
            return "Yesterday"
        return when.strftime("%a, %d %b %Y")

    def _format_message_time(self, when: Optional[datetime], fallback: str) -> str:
        if when is None:
            return fallback
        return when.strftime("%H:%M")

    def _on_delete_message(self, message, row: QWidget) -> None:
        if row is None or self._is_shutting_down:
            return
        self._hidden_messages.add(self._message_hide_key(message))
        self.chat_layout.removeWidget(row)
        row.deleteLater()

    def _on_message_link(self, value: str) -> None:
        if value.startswith("download:"):
            query = unquote(value.split(":", 1)[1].strip())
            target = resolve_download_target(self.session.available_files, query)
            if not target:
                self._append_system_message("Download failed: file not found in recent messages")
                return
            name, relative_path = target
            self._download_file(name, relative_path)
            return
        if value.startswith("open:"):
            local_path = unquote(value.split(":", 1)[1].strip())
            self._open_local_path(local_path)
            return

    def _open_local_path(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.exists():
            self._append_system_message(f"Open failed: {target} not found")
            return
        if sys.platform == "darwin":
            cmd = ["open", str(target)]
        elif shutil.which("xdg-open"):
            cmd = ["xdg-open", str(target)]
        else:
            self._append_system_message("Open failed: no opener command found")
            return
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _poll(self) -> None:
        if self._is_shutting_down or self.read_busy:
            return
        self.read_busy = True

        def _read_cycle():
            return self.transport.read_messages(200)

        if not self._enqueue_transport_task("poll", _read_cycle, self._on_poll_finished, dedupe=True):
            self.read_busy = False

    def _on_poll_finished(self, result, error) -> None:
        self.read_busy = False
        if self._is_shutting_down:
            return
        if error:
            log.warning("Poll error: %s", error)
            self.transport.last_error = str(error)
            self._render_status(self.transport.status, self.transport.last_error)
            return
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            log.error("Poll returned unexpected result: %r", result)
            return
        snapshot, statuses = result
        log.debug("Poll ok, snapshot=%s, statuses=%s", snapshot is not None, statuses)
        self._render_status(statuses, self.transport.last_error)
        if snapshot is not None and not self._online_refresh_started:
            self._online_refresh_started = True
            QTimer.singleShot(ONLINE_FIRST_REFRESH_DELAY_MS, self._start_online_refresh_cycle)
        if snapshot is not None and snapshot != self.session.last_snapshot:
            rendered = self.session.render_snapshot(snapshot)
            self._render_messages(rendered.messages)
            if rendered.should_play_notification:
                self._play_incoming_sound()
        if self.session.should_retry_pending(self.transport.mode, snapshot):
            self._retry_pending_once()

    def _start_online_refresh_cycle(self) -> None:
        if self._is_shutting_down:
            return
        self._refresh_online_users()
        self.online_timer.start(ONLINE_REFRESH_INTERVAL_MS)

    def _refresh_online_users(self) -> None:
        if self._is_shutting_down or self.online_busy:
            return
        self.online_busy = True

        def _online_cycle():
            self.transport.touch_presence(self.session.display_name)
            return self.transport.fetch_online_users(ONLINE_PRESENCE_WINDOW_SECONDS)

        if not self._enqueue_transport_task("online_refresh", _online_cycle, self._on_online_users_finished, dedupe=True):
            self.online_busy = False

    def _on_online_users_finished(self, result, error) -> None:
        self.online_busy = False
        if self._is_shutting_down:
            return
        if error:
            log.warning("Online-users error: %s", error)
            return
        self.server_online_users = result or []
        self._render_online_users()

    def _retry_pending_once(self) -> None:
        if self._is_shutting_down or self.retry_busy or not self.session.pending_messages:
            return
        self.retry_busy = True
        user, text = self.session.pending_messages[0]
        if not self._enqueue_transport_task(
            "retry_send",
            lambda: self.transport.send_message(user, text),
            lambda result, error: self._on_retry_finished(user, text, result, error),
            dedupe=True,
        ):
            self.retry_busy = False

    def _on_retry_finished(self, user: str, text: str, result, error) -> None:
        self.retry_busy = False
        if self._is_shutting_down:
            return
        if error:
            log.warning("Retry-send error: %s", error)
            self.transport.last_error = str(error)
            self._render_status(self.transport.status, self.transport.last_error)
            return
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            log.error("Retry-send returned unexpected result: %r", result)
            return
        ok, _err, statuses = result
        log.debug("Retry-send ok=%s", ok)
        self._render_status(statuses, self.transport.last_error)
        if not ok:
            return
        self.session.remove_pending_message(user, text)
        self._poll()

    def _show_emoji_picker(self) -> None:
        if self.emoji_picker and self.emoji_picker.isVisible():
            self.emoji_picker.close()
            return
        self.emoji_picker = EmojiPickerDialog(self, self._insert_emoji)
        self.emoji_picker.adjustSize()
        anchor = self.emoji_btn.mapToGlobal(self.emoji_btn.rect().topRight())
        x_pos = anchor.x() - self.emoji_picker.width() + self.emoji_btn.width()
        y_pos = anchor.y() - self.emoji_picker.height() - 6
        self.emoji_picker.move(x_pos, y_pos)
        self.emoji_picker.show()

    def _insert_emoji(self, emoji: str) -> None:
        cursor_pos = self.input_line.cursorPosition()
        value = self.input_line.text()
        updated = value[:cursor_pos] + emoji + value[cursor_pos:]
        self.input_line.setText(updated)
        self.input_line.setCursorPosition(cursor_pos + len(emoji))
        self.input_line.setFocus()

    def _send_input(self) -> None:
        text = self.input_line.text().strip()
        self.input_line.clear()
        if not text:
            return
        if not text.startswith("/upload "):
            upload_command = as_upload_command(text)
            if upload_command:
                text = upload_command

        if text == "/clear":
            self._clear_chat()
            return
        if text.startswith("/news "):
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                self._append_system_message("News failed: usage /news ChannelName 10 or /news ChannelName 20-10 or /news ChannelName 20 10")
                return
            channel = parts[1].strip()
            range_spec = parts[2].strip() if len(parts) > 2 else "10"
            if not channel:
                self._append_system_message("News failed: channel name required")
                return
            self._fetch_news(channel, range_spec)
            return
        if text.startswith("/upload "):
            self._upload_file(text.split(" ", 1)[1].strip())
            return
        if text.startswith("/download "):
            query = text.split(" ", 1)[1].strip()
            target = resolve_download_target(self.session.available_files, query)
            if not target:
                self._append_system_message("Download failed: file not found in recent messages")
                return
            name, relative_path = target
            self._download_file(name, relative_path)
            return
        self._send_text(text)

    def _send_text(self, text: str) -> None:
        if self._is_shutting_down:
            return
        messages = self.session.append_pending_message(text)
        self._render_messages(messages)
        self._enqueue_transport_task(
            "send_message",
            lambda: self.transport.send_message(self.session.display_name, text),
            lambda result, error: self._on_send_finished(text, result, error),
        )

    def _on_send_finished(self, text: str, result, error) -> None:
        if self._is_shutting_down:
            return
        if error:
            log.warning("Send error: %s", error)
            self._append_system_message(f"Send failed: {error}")
            return
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            log.error("Send returned unexpected result: %r", result)
            self._append_system_message("Send failed: unexpected transport response")
            return
        ok, err, statuses = result
        log.debug("Send ok=%s err=%s", ok, err)
        self._render_status(statuses, self.transport.last_error)
        if not ok:
            self._append_system_message(f"Send failed: {err or 'no working link'}")
            return
        self.session.remove_pending_message(self.session.display_name, text)
        self._play_outgoing_sound()
        self._poll()

    def _clear_chat(self) -> None:
        self._enqueue_transport_task("clear_chat", self.transport.clear_messages, self._on_clear_finished)

    def _on_clear_finished(self, result, error) -> None:
        if self._is_shutting_down:
            return
        if error:
            log.warning("Clear error: %s", error)
            self._append_system_message(f"Clear failed: {error}")
            return
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            log.error("Clear returned unexpected result: %r", result)
            self._append_system_message("Clear failed: unexpected response")
            return
        ok, err = result
        if ok:
            self.session.clear_messages()
            self._clear_chat_widgets()
            self._append_system_message("Chat cleared")
        else:
            self._append_system_message(f"Clear failed: {err}")

    def _fetch_news(self, channel: str, range_spec: str) -> None:
        self._append_system_message(f"Fetching news: {channel} {range_spec}")
        self._enqueue_transport_task(
            "fetch_news",
            lambda: self.transport.fetch_news(channel, range_spec),
            lambda result, error: self._on_news_finished(channel, range_spec, result, error),
        )

    def _on_news_finished(self, channel: str, range_spec: str, result, error) -> None:
        if self._is_shutting_down:
            return
        if error:
            log.warning("News error: %s", error)
            self._append_system_message(f"News failed: {error}")
            return
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            log.error("News returned unexpected result: %r", result)
            self._append_system_message("News failed: unexpected response")
            return
        ok, output, statuses = result
        self._render_status(statuses, self.transport.last_error)
        if not ok:
            self._append_system_message(f"News failed: {output}")
            return
        self._append_system_message(f"News imported: {channel} {range_spec}")
        self._poll()

    def _attach_file(self) -> None:
        path = self._pick_file("Select file to upload")
        if not path:
            return
        self._upload_file(path)

    def _pick_file(self, title: str, start_dir: Optional[Path] = None) -> str:
        base_dir = (start_dir or (Path.home() / "Desktop")).expanduser()
        if not base_dir.exists() or not base_dir.is_dir():
            base_dir = Path.home()
        dialog = QFileDialog(self, title)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setFileMode(QFileDialog.ExistingFile)
        dialog.setDirectory(str(base_dir))
        if dialog.exec():
            files = dialog.selectedFiles()
            if files:
                return files[0]
        return ""

    def _upload_file(self, path: str) -> None:
        self._append_system_message(f"Uploading: {path}")
        self.transfer_label.setText(self.session.clear_upload_status())
        self.transfer_bar.setValue(0)
        self.transfer_bar.hide()

        def progress_cb(label, percent, stage):
            if self._is_shutting_down:
                return
            self.progress_signal.emit(label, percent, stage)

        def _run():
            return self.transport.upload_file(path, progress_cb)

        self._enqueue_transport_task("upload_file", _run, self._on_upload_finished)

    def _on_upload_finished(self, result, error) -> None:
        if self._is_shutting_down:
            return
        self.transfer_label.setText(self.session.clear_upload_status())
        self.transfer_bar.hide()
        if error:
            log.warning("Upload error: %s", error)
            self._append_system_message(f"Upload failed: {error}")
            return
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            log.error("Upload returned unexpected result: %r", result)
            self._append_system_message("Upload failed: unexpected response")
            return
        ok, output, statuses = result
        self._render_status(statuses, self.transport.last_error)
        if not ok:
            self._append_system_message(f"Upload failed: {output}")
            return
        self._append_system_message(f"Upload complete: {output}")
        self._send_text(f"[file] {output}::uploads/{output}")

    def _download_file(self, name: str, relative_path: str) -> None:
        self._append_system_message(f"Downloading: {name}")
        self.transfer_label.setText(self.session.clear_upload_status())
        self.transfer_bar.setValue(0)
        self.transfer_bar.hide()

        def progress_cb(label, percent, stage):
            if self._is_shutting_down:
                return
            self.progress_signal.emit(label, percent, stage)

        def _run():
            return self.transport.download_file(
                relative_path,
                progress_cb,
            )

        self._enqueue_transport_task(
            "download_file",
            _run,
            lambda result, error: self._on_download_finished(name, result, error),
        )

    def _on_download_finished(self, name: str, result, error) -> None:
        if self._is_shutting_down:
            return
        self.transfer_label.setText(self.session.clear_upload_status())
        self.transfer_bar.hide()
        if error:
            log.warning("Download error: %s", error)
            self._append_system_message(f"Download failed: {error}")
            return
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            log.error("Download returned unexpected result: %r", result)
            self._append_system_message("Download failed: unexpected response")
            return
        ok, output, statuses = result
        self._render_status(statuses, self.transport.last_error)
        if not ok:
            self._append_system_message(f"Download failed: {output}")
            return
        safe_name = html.escape(name, quote=True)
        safe_output = html.escape(output, quote=True)
        open_href = quote(output, safe="")
        self._append_system_message(
            f'✅ {safe_name}  <a href="open:{open_href}">📂 Open</a>',
            is_html=True,
        )

    def _on_progress_update(self, label: str, percent, stage: str) -> None:
        if self._is_shutting_down:
            return
        text = self.session.update_upload_status(label, percent, stage)
        self.transfer_label.setText(text)
        if stage == "done":
            self.transfer_bar.setRange(0, 100)
            self.transfer_bar.setValue(100)
            self.transfer_bar.show()
            return
        if percent is not None:
            self.transfer_bar.setRange(0, 100)
            self.transfer_bar.setValue(max(0, min(100, int(percent))))
            self.transfer_bar.show()
            return
        if stage in {"preparing", "probing", "selected", "starting", "uploading"}:
            self.transfer_bar.setRange(0, 0)
            self.transfer_bar.show()
            return
        self.transfer_bar.hide()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        for url in event.mimeData().urls():
            if url.isLocalFile():
                local_path = url.toLocalFile()
                if Path(local_path).is_file():
                    self._upload_file(local_path)
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)

    def _start_slipstream_clients(self) -> None:
        self.slipstream_manager = SlipstreamManager(
            slip_path=self.config["slip_path"],
            domain=self.config["domain"],
            dns_ips=self.config["dns_ips"],
            proxy_ports=self.config["proxy_ports"],
        )
        worker = FunctionWorker(
            lambda: self.slipstream_manager.start(lambda text: self.system_signal.emit(text))
        )
        self._slip_worker = worker

        def _on_done(_result, error):
            self._slip_worker = None
            if self._is_shutting_down:
                return
            self._on_slipstream_started(error)

        worker.signals.finished.connect(_on_done)
        QThreadPool.globalInstance().start(worker)

    def _on_slipstream_started(self, error) -> None:
        if error:
            log.error("Slipstream startup error: %s", error)
            self._append_system_message(f"Slipstream startup error: {error}")
            return
        log.info("Slipstream ready")
        self._poll()

    def _shutdown(self) -> None:
        log.info("Shutdown started")
        self._is_shutting_down = True
        self._transport_queue.clear()
        if hasattr(self, "poll_timer") and self.poll_timer:
            self.poll_timer.stop()
        if hasattr(self, "online_timer") and self.online_timer:
            self.online_timer.stop()
        if self._active_worker:
            self._active_worker.cancel()
        if getattr(self, "_slip_worker", None):
            self._slip_worker.cancel()
        log.info("Waiting for thread pool (5s timeout)")
        self.thread_pool.waitForDone(5000)
        if self.slipstream_manager:
            log.info("Stopping slipstream processes")
            self.slipstream_manager.stop()
        log.info("Shutdown complete")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._shutdown()
        if self.on_close_callback:
            self.on_close_callback()
        super().closeEvent(event)


class LauncherWindow(QMainWindow):
    def __init__(self, dns_file: Optional[str], initial_state: Optional[dict], state_path: Path):
        super().__init__()
        self.dns_file = dns_file or str(Path.home() / "Desktop" / "Projects" / "test-dns" / "result.txt")
        self.initial_state = initial_state or {}
        self.state_path = state_path
        self.chat_window: Optional[ChatWindow] = None
        self.scanner_proc: Optional[subprocess.Popen] = None
        self._scanner_output_path = APP_STATE_ROOT / "scanner-result.txt"
        self._scanner_known_ips: set = set()
        self._scan_finished_at: Optional[float] = None
        self.scanner_timer = QTimer(self)
        self.scanner_timer.timeout.connect(self._poll_scanner_result)
        self.setWindowTitle("Chat over DNSTT")
        self.resize(760, 650)
        icon_path = _app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        self._setup_ui()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        title = QLabel("Vitalize Secure Chat")
        title.setObjectName("LauncherTitle")
        subtitle = QLabel("Connect with SSH or DNSTT")
        subtitle.setObjectName("LauncherSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        tabs = QTabWidget()
        tabs.setObjectName("LauncherTabs")
        self.ssh_tab = QWidget()
        self.dns_tab = QWidget()
        tabs.addTab(self.ssh_tab, "SSH")
        tabs.addTab(self.dns_tab, "DNSTT")
        layout.addWidget(tabs, 1)
        self._build_ssh_tab()
        self._build_dns_tab()
        footer = QLabel("Copyright by Vitalize 2026")
        footer.setObjectName("LauncherCopyright")
        footer.setAlignment(Qt.AlignCenter)
        layout.addWidget(footer)
        self.setStyleSheet(
            """
            QMainWindow { background: #05060a; color: #e2e8f0; }
            QLabel#LauncherTitle { font-size: 26px; font-weight: 700; color: #f8fafc; }
            QLabel#LauncherSubtitle { color: #93a3b8; margin-bottom: 2px; }
            QTabWidget#LauncherTabs::pane { border: 1px solid #334155; border-radius: 14px; top: -1px; background: #0b1220; }
            QTabBar::tab {
                background: #111827; color: #cbd5e1; padding: 10px 16px; border: 1px solid #334155;
                border-top-left-radius: 10px; border-top-right-radius: 10px; margin-right: 6px;
            }
            QTabBar::tab:selected { background: #1d2433; color: #ffffff; border-color: #0ea5e9; }
            QLineEdit {
                border: 1px solid #334155; border-radius: 10px; padding: 9px;
                background: #0b1220; color: #f8fafc;
            }
            QPushButton {
                border: 1px solid #475569; border-radius: 10px; padding: 9px 14px;
                background: #1f2937; color: #f8fafc;
            }
            QPushButton:hover { background: #2b3648; border-color: #38bdf8; }
            QFrame#LauncherCard {
                background: rgba(17, 24, 39, 0.92);
                border: 1px solid #334155;
                border-radius: 14px;
            }
            QLabel#CardHint { color: #9fb3c8; }
            QListWidget { border: 1px solid #334155; border-radius: 10px; background: #0a1323; }
            QLabel#LauncherCopyright { color: #64748b; font-size: 11px; }
            """
        )

    def _line(self, placeholder: str, value: str = "", password: bool = False) -> QLineEdit:
        line = QLineEdit()
        line.setPlaceholderText(placeholder)
        line.setText(value)
        if password:
            line.setEchoMode(QLineEdit.Password)
        return line

    def _ssh_secret_key(self, host: str, user: str) -> str:
        return f"ssh:{host.strip().lower()}:{user.strip().lower()}"

    def _dns_secret_key(self, domain: str, user: str) -> str:
        return f"dns:{domain.strip().lower()}:{user.strip().lower()}"

    def _build_ssh_tab(self) -> None:
        state = self.initial_state.get("ssh", {})
        tab_layout = QVBoxLayout(self.ssh_tab)
        tab_layout.setContentsMargins(14, 14, 14, 14)
        tab_layout.setSpacing(10)
        card = QFrame()
        card.setObjectName("LauncherCard")
        tab_layout.addWidget(card)
        tab_layout.addStretch(1)
        form = QGridLayout(card)
        form.setContentsMargins(14, 14, 14, 14)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        hint = QLabel("Direct SSH chat session")
        hint.setObjectName("CardHint")
        form.addWidget(hint, 0, 0, 1, 2)
        self.ssh_host = self._line("Host (e.g. 65.109.217.21)", state.get("host", ""))
        self.ssh_user = self._line("Username", state.get("user", ""))
        ssh_remembered = bool(state.get("remember_password", False))
        cached_ssh_pass = ""
        if ssh_remembered:
            cached_ssh_pass = load_secure_password(
                self._ssh_secret_key(state.get("host", ""), state.get("user", ""))
            )
        self.ssh_pass = self._line("Password", cached_ssh_pass, password=True)
        self.ssh_remember = QCheckBox("Remember password")
        self.ssh_remember.setChecked(ssh_remembered)
        self.ssh_name = self._line("Display name", state.get("name", ""))
        self.ssh_remote = self._line("Remote chat.sh path", state.get("remote_script", DEFAULT_REMOTE_SCRIPT))
        row = 1
        for label, widget in [
            ("Host", self.ssh_host),
            ("Username", self.ssh_user),
            ("Password", self.ssh_pass),
            ("", self.ssh_remember),
            ("Display name", self.ssh_name),
            ("Remote script", self.ssh_remote),
        ]:
            form.addWidget(QLabel(label) if label else QLabel(""), row, 0)
            form.addWidget(widget, row, 1)
            row += 1
        btn = QPushButton("Connect SSH")
        btn.clicked.connect(self._connect_ssh)
        form.addWidget(btn, row, 1, alignment=Qt.AlignRight)

    def _build_dns_tab(self) -> None:
        state = self.initial_state.get("dns", {})
        tab_layout = QVBoxLayout(self.dns_tab)
        tab_layout.setContentsMargins(14, 14, 14, 14)
        tab_layout.setSpacing(10)
        card = QFrame()
        card.setObjectName("LauncherCard")
        tab_layout.addWidget(card)
        tab_layout.addStretch(1)
        form = QGridLayout(card)
        form.setContentsMargins(14, 14, 14, 14)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        hint = QLabel("DNSTT multi-link tunnel session")
        hint.setObjectName("CardHint")
        form.addWidget(hint, 0, 0, 1, 3)

        default_slip = str(Path.home() / "Desktop" / "slipstream-rust")
        self.dns_slip_path = self._line("Slipstream path", state.get("slip_path", default_slip))
        self.dns_domain = self._line("Domain", state.get("domain", DEFAULT_DOMAIN))
        self.dns_user = self._line("Username", state.get("user", ""))
        dns_remembered = bool(state.get("remember_password", False))
        cached_dns_pass = ""
        if dns_remembered:
            cached_dns_pass = load_secure_password(
                self._dns_secret_key(state.get("domain", DEFAULT_DOMAIN), state.get("user", ""))
            )
        self.dns_pass = self._line("Password", cached_dns_pass, password=True)
        self.dns_remember = QCheckBox("Remember password")
        self.dns_remember.setChecked(dns_remembered)
        self.dns_name = self._line("Display name", state.get("name", ""))
        self.dns_remote = self._line("Remote chat.sh path", state.get("remote_script", DEFAULT_REMOTE_SCRIPT))
        self.dns_file_path = self._line("DNS result file", state.get("dns_file_path", self.dns_file))
        self.dns_extra = self._line("Extra DNS IPs (comma-separated)", state.get("dns_extra", ""))
        self.dns_scan_input = self._line("Scanner input file (optional)", state.get("scanner_input_file", ""))
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_dns_file)
        scan_input_browse_btn = QPushButton("Browse")
        scan_input_browse_btn.clicked.connect(self._browse_scan_input_file)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._start_scan)
        self.scan_status = QLabel("Not scanned yet")
        self.scan_status.setObjectName("CardHint")

        self.dns_ip_list = QListWidget()
        self.dns_ip_list.setSelectionMode(QListWidget.NoSelection)
        self._load_dns_ips(self.dns_file_path.text())

        row = 1
        for label, widget in [
            ("Slipstream path", self.dns_slip_path),
            ("Domain", self.dns_domain),
            ("Username", self.dns_user),
            ("Password", self.dns_pass),
            ("", self.dns_remember),
            ("Display name", self.dns_name),
            ("Remote script", self.dns_remote),
        ]:
            form.addWidget(QLabel(label) if label else QLabel(""), row, 0)
            form.addWidget(widget, row, 1, 1, 2)
            row += 1

        form.addWidget(QLabel("DNS file"), row, 0)
        form.addWidget(self.dns_file_path, row, 1)
        form.addWidget(browse_btn, row, 2)
        row += 1
        form.addWidget(QLabel("Scan input"), row, 0)
        form.addWidget(self.dns_scan_input, row, 1)
        form.addWidget(scan_input_browse_btn, row, 2)
        row += 1
        form.addWidget(QLabel("Scanner"), row, 0)
        scan_row = QHBoxLayout()
        scan_row.setContentsMargins(0, 0, 0, 0)
        scan_row.setSpacing(8)
        scan_row.addWidget(self.scan_btn)
        scan_row.addWidget(self.scan_status, 1)
        form.addLayout(scan_row, row, 1, 1, 2)
        row += 1
        form.addWidget(QLabel("DNS IPs"), row, 0, alignment=Qt.AlignTop)
        form.addWidget(self.dns_ip_list, row, 1, 1, 2)
        row += 1
        form.addWidget(QLabel("Extra IPs"), row, 0)
        form.addWidget(self.dns_extra, row, 1, 1, 2)
        row += 1

        btn = QPushButton("Connect DNSTT")
        btn.clicked.connect(self._connect_dns)
        form.addWidget(btn, row, 2, alignment=Qt.AlignRight)

    def _browse_dns_file(self) -> None:
        suggested = Path(self.dns_file_path.text().strip()).expanduser()
        if suggested.exists():
            start_dir = suggested.parent if suggested.is_file() else suggested
        else:
            desktop = Path.home() / "Desktop"
            start_dir = desktop if desktop.exists() else Path.home()
        path = self._pick_file("Select DNS result file", start_dir=start_dir)
        if not path:
            return
        self.dns_file_path.setText(path)
        # Browsing a new list should fully replace previous DNS inputs.
        self.dns_extra.clear()
        self._load_dns_ips(path)

    def _browse_scan_input_file(self) -> None:
        suggested = Path(self.dns_scan_input.text().strip()).expanduser()
        if suggested.exists():
            start_dir = suggested.parent if suggested.is_file() else suggested
        else:
            desktop = Path.home() / "Desktop"
            start_dir = desktop if desktop.exists() else Path.home()
        path = self._pick_file("Select scanner input file", start_dir=start_dir)
        if not path:
            return
        self.dns_scan_input.setText(path)

    def _start_scan(self) -> None:
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self.scan_status.setText("Scanning...")
            return
        scanner_script = PROJECT_ROOT / "scanner.py"
        input_file = Path(self.dns_scan_input.text().strip()).expanduser()
        if not input_file.exists():
            self._show_error("Scanner input file not found")
            return
        if not scanner_script.exists():
            self._show_error("scanner.py not found in project root")
            return
        self._scanner_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._scanner_output_path.write_text("")
        self._scanner_known_ips.clear()
        cmd = [
            sys.executable,
            str(scanner_script),
            "-f",
            str(input_file),
            "-o",
            str(self._scanner_output_path),
        ]
        self.scanner_proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._scan_finished_at = None
        self.scan_status.setText("Scanning...")
        self.scanner_timer.start(SCANNER_POLL_INTERVAL_MS)

    def _poll_scanner_result(self) -> None:
        if self._scanner_output_path.exists():
            entries = parse_dns_result_file(str(self._scanner_output_path))
            for ip, _stamp in entries:
                if ip not in self._scanner_known_ips:
                    self._scanner_known_ips.add(ip)
            if self._scanner_known_ips:
                self._load_dns_ips(self.dns_file_path.text())
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self.scan_status.setText("Scanning...")
            return
        if self.scanner_proc:
            if self._scan_finished_at is None:
                self._scan_finished_at = time.time()
                self.scanner_timer.setInterval(60_000)
            elapsed = max(0, int(time.time() - self._scan_finished_at))
            if elapsed < 60:
                age = "just now"
            elif elapsed < 3600:
                age = f"{elapsed // 60}m ago"
            elif elapsed < 86400:
                age = f"{elapsed // 3600}h ago"
            else:
                age = f"{elapsed // 86400}d ago"
            self.scan_status.setText(f"Scanned: {age} ({len(self._scanner_known_ips)} found)")
        else:
            self.scan_status.setText("Not scanned yet")
            self.scanner_timer.stop()

    def _pick_file(self, title: str, start_dir: Optional[Path] = None) -> str:
        base_dir = (start_dir or (Path.home() / "Desktop")).expanduser()
        if not base_dir.exists() or not base_dir.is_dir():
            base_dir = Path.home()
        dialog = QFileDialog(self, title)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setFileMode(QFileDialog.ExistingFile)
        dialog.setDirectory(str(base_dir))
        if dialog.exec():
            files = dialog.selectedFiles()
            if files:
                return files[0]
        return ""

    def _load_dns_ips(self, path: str) -> None:
        self.dns_ip_list.clear()
        self.dns_ip_list.clearSelection()
        entries = parse_dns_result_file(path)
        scanner_entries = parse_dns_result_file(str(self._scanner_output_path))
        merged: List[Tuple[str, str]] = []
        seen = set()
        for ip, stamp in entries + scanner_entries:
            if ip and ip not in seen:
                seen.add(ip)
                merged.append((ip, stamp))
        if not merged:
            item = QListWidgetItem("(no file/scan results - browse, scan, or add IPs below)")
            item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            self.dns_ip_list.addItem(item)
            return
        for ip, stamp in merged:
            label = f"{ip} ({stamp})" if stamp else ip
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ip)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.dns_ip_list.addItem(item)

    def _selected_dns_ips(self) -> List[str]:
        ips: List[str] = []
        for idx in range(self.dns_ip_list.count()):
            item = self.dns_ip_list.item(idx)
            ip = item.data(Qt.UserRole)
            if ip and item.checkState() == Qt.Checked:
                ips.append(ip)
        extra = self.dns_extra.text().strip()
        if extra:
            for ip in extra.replace(",", " ").split():
                value = ip.strip()
                if value and value not in ips:
                    ips.append(value)
        return ips

    def _show_error(self, text: str) -> None:
        QMessageBox.critical(self, "Error", text)

    def _connect_ssh(self) -> None:
        values = {
            "host": self.ssh_host.text().strip(),
            "user": self.ssh_user.text().strip(),
            "password": self.ssh_pass.text().strip(),
            "name": self.ssh_name.text().strip(),
            "remote_script": self.ssh_remote.text().strip() or DEFAULT_REMOTE_SCRIPT,
            "remember_password": self.ssh_remember.isChecked(),
        }
        if not values["host"] or not values["user"]:
            self._show_error("Host and user required")
            return
        ssh_key = self._ssh_secret_key(values["host"], values["user"])
        if values["remember_password"] and values["password"]:
            save_secure_password(ssh_key, values["password"])
        elif not values["remember_password"]:
            delete_secure_password(ssh_key)
        save_launcher_state(self.state_path, values)
        config = {
            "mode": "ssh",
            "host": values["host"],
            "user": values["user"],
            "password": values["password"],
            "name": values["name"],
            "remote_script": values["remote_script"],
        }
        self._open_chat(config, startup_lines=[])

    def _connect_dns(self) -> None:
        slip_path = Path(self.dns_slip_path.text().strip()).expanduser()
        values = {
            "slip_path": slip_path,
            "domain": self.dns_domain.text().strip() or DEFAULT_DOMAIN,
            "user": self.dns_user.text().strip(),
            "password": self.dns_pass.text().strip(),
            "name": self.dns_name.text().strip(),
            "remote_script": self.dns_remote.text().strip() or DEFAULT_REMOTE_SCRIPT,
            "dns_file_path": self.dns_file_path.text().strip(),
            "scanner_input_file": self.dns_scan_input.text().strip(),
            "dns_extra": self.dns_extra.text().strip(),
            "ips": self._selected_dns_ips(),
            "remember_password": self.dns_remember.isChecked(),
        }
        if not values["user"]:
            self._show_error("Username required")
            return
        if not values["ips"]:
            self._show_error("Select at least one DNS IP")
            return
        if not values["slip_path"] or not values["slip_path"].exists():
            self._show_error("Slipstream path must exist")
            return
        dns_key = self._dns_secret_key(values["domain"], values["user"])
        if values["remember_password"] and values["password"]:
            save_secure_password(dns_key, values["password"])
        elif not values["remember_password"]:
            delete_secure_password(dns_key)
        save_launcher_state(self.state_path, values)
        base_port = 8000
        config = {
            "mode": "dns",
            "slip_path": str(values["slip_path"]),
            "domain": values["domain"],
            "user": values["user"],
            "password": values["password"],
            "name": values["name"],
            "remote_script": values["remote_script"],
            "dns_ips": values["ips"],
            "proxy_ports": [base_port + idx for idx in range(len(values["ips"]))],
        }
        startup_lines = [
            "Preparing DNSTT chat session...",
            f"Using {len(values['ips'])} DNS link(s).",
        ]
        self._open_chat(config, startup_lines=startup_lines)

    def _open_chat(self, config: dict, startup_lines: List[str]) -> None:
        if self.chat_window:
            self.chat_window.close()
        self.chat_window = ChatWindow(
            config=config,
            startup_lines=startup_lines,
            on_close_callback=self.show,
        )
        self.chat_window.show()
        self.hide()


def main() -> None:
    _setup_crash_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dns-file", default="", help="Path to DNS result file")
    args = parser.parse_args()
    dns_file = args.dns_file or str(PROJECT_ROOT / ".." / "test-dns" / "result.txt")
    if not Path(dns_file).exists():
        dns_file = ""
    state_path = APP_STATE_ROOT / ".launcher_state.json"
    initial_state = load_launcher_state(state_path)

    app = QApplication(sys.argv)
    app.setApplicationName("chat-over-dnstt")
    _configure_fonts(app)
    icon_path = _app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = LauncherWindow(dns_file=dns_file, initial_state=initial_state, state_path=state_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

