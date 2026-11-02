from PySide6.QtCore import Qt, Signal, QThread, Slot, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.antigravity_worker import AntigravityWorker
from ui.system_dashboard import SystemDashboardWidget
from ui.widgets import BubbleRow, MessageBubble, ThinkingIndicator


class ChatWindow(QWidget):
    """Modern dark-themed JARVIS desktop chat interface with message bubbles,
    streaming Markdown support, animated thinking indicator, and smart auto-scroll."""

    message_requested = Signal(str)
    weather_refresh_requested = Signal()
    shutdown_requested = Signal()
    state_changed = Signal(str)
    token_streamed = Signal()
    shutdown_finished = Signal()

    SCROLL_BOTTOM_THRESHOLD = 28

    def __init__(self, start_worker: bool = True):
        super().__init__()

        self.setWindowTitle("JARVIS Assistant")
        self.setMinimumSize(450, 580)
        self.resize(500, 680)

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._user_scrolled_up = False
        self._programmatic_scroll = False
        self._active_assistant_bubble: MessageBubble | None = None
        self._bubbles: list[MessageBubble] = []
        self._shutting_down = False
        self._shutdown_complete = False
        self._auto_refresh_weather = start_worker

        self._apply_stylesheet()
        self._build_ui()

        # Worker thread setup
        self.worker_thread = QThread(self)
        self.worker = AntigravityWorker()
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.initialize)
        self.message_requested.connect(self.worker.send_message)
        if hasattr(self.worker, "refresh_weather_badge"):
            self.weather_refresh_requested.connect(self.worker.refresh_weather_badge)

        self.worker.ready.connect(self.on_ready)
        self.worker.token_received.connect(self.on_token)
        self.worker.response_finished.connect(self.on_finished)
        self.worker.error_occurred.connect(self.on_error)

        if hasattr(self.worker, "state_changed"):
            self.worker.state_changed.connect(self._on_worker_state_changed)
        if hasattr(self.worker, "thinking_started"):
            self.worker.thinking_started.connect(self._on_thinking_started)
        if hasattr(self.worker, "shutdown_completed"):
            self.worker.shutdown_completed.connect(self._on_worker_shutdown_completed)
        if hasattr(self.worker, "weather_badge_updated"):
            self.worker.weather_badge_updated.connect(self.update_weather_badge)

        self.shutdown_requested.connect(self.worker.shutdown)
        self.worker_thread.finished.connect(self._on_thread_finished)
        if hasattr(self, "system_dashboard") and self.system_dashboard is not None:
            self.system_dashboard.monitor_thread.finished.connect(self._on_thread_finished)

        if start_worker:
            self.worker_thread.start()

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #070b14;
                color: #e8f4ff;
                font-family: 'Ubuntu', 'Segoe UI', sans-serif;
            }
            QFrame#headerFrame {
                background-color: #0c1425;
                border: 1px solid #1b2c47;
                border-radius: 14px;
            }
            QLabel#title {
                color: #38bdf8;
                font-size: 20px;
                font-weight: bold;
                letter-spacing: 1.5px;
                background: transparent;
            }
            QLabel#subtitle {
                color: #64748b;
                font-size: 10.5px;
                font-weight: 600;
                letter-spacing: 0.6px;
                background: transparent;
            }
            QLabel#status {
                background-color: #10233a;
                border: 1px solid #1e3a5f;
                border-radius: 10px;
                padding: 4px 10px;
                color: #34d399;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton#weatherButton {
                background-color: #10243d;
                border: 1px solid #1e3f66;
                border-radius: 8px;
                padding: 5px 10px;
                color: #7dd3fc;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#weatherButton:hover {
                background-color: #163254;
                color: #e0f2fe;
                border-color: #38bdf8;
            }
            QPushButton#systemButton {
                background-color: #10243d;
                border: 1px solid #1e3f66;
                border-radius: 8px;
                padding: 5px 10px;
                color: #38bdf8;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#systemButton:hover {
                background-color: #163254;
                color: #e0f2fe;
                border-color: #38bdf8;
            }
            QPushButton#clearButton {
                background-color: #132036;
                border: 1px solid #243b5e;
                border-radius: 8px;
                padding: 5px 10px;
                color: #94a3b8;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#clearButton:hover {
                background-color: #1c2f4d;
                color: #e2e8f0;
                border-color: #38bdf8;
            }
            QScrollArea#chatScrollArea {
                background-color: #0a101f;
                border: 1px solid #18263e;
                border-radius: 14px;
            }
            QWidget#messagesContainer {
                background-color: #0a101f;
            }
            QScrollBar:vertical {
                background: #090e1c;
                width: 8px;
                margin: 4px 2px 4px 2px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #233858;
                min-height: 28px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #38bdf8;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QFrame#inputFrame {
                background-color: #0c1425;
                border: 1px solid #1b2c47;
                border-radius: 14px;
            }
            QLineEdit#chatInput {
                background-color: #132038;
                border: 1px solid #264066;
                border-radius: 10px;
                padding: 11px 14px;
                color: #f1f5f9;
                font-size: 13.5px;
            }
            QLineEdit#chatInput:focus {
                border: 1px solid #38bdf8;
                background-color: #162642;
            }
            QLineEdit#chatInput:disabled {
                background-color: #0e1726;
                color: #64748b;
                border-color: #1a2940;
            }
            QPushButton#sendButton {
                background-color: #0284c7;
                border: 1px solid #38bdf8;
                border-radius: 10px;
                padding: 10px 18px;
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#sendButton:hover {
                background-color: #0ea5e9;
            }
            QPushButton#sendButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }
        """)

    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        # Header Card
        header_frame = QFrame(self)
        header_frame.setObjectName("headerFrame")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        title = QLabel("J.A.R.V.I.S.")
        title.setObjectName("title")
        subtitle = QLabel("GEMINI 4 ARGON • PLAN MODE")
        subtitle.setObjectName("subtitle")

        title_col.addWidget(title)
        title_col.addWidget(subtitle)

        header_layout.addLayout(title_col)
        header_layout.addStretch()

        self.weather_button = QPushButton("⛅ Hava")
        self.weather_button.setObjectName("weatherButton")
        self.weather_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.weather_button.setToolTip("Hava durumu raporunu göster")
        self.weather_button.clicked.connect(self.request_weather_report)
        header_layout.addWidget(self.weather_button)

        self.system_button = QPushButton("🖥️ Sistem")
        self.system_button.setObjectName("systemButton")
        self.system_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.system_button.setToolTip("Sistem İstihbarat Panelini Aç/Kapat")
        self.system_button.clicked.connect(self.toggle_system_dashboard)
        header_layout.addWidget(self.system_button)

        self.clear_button = QPushButton("Temizle")
        self.clear_button.setObjectName("clearButton")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear_messages)
        header_layout.addWidget(self.clear_button)

        self.status = QLabel("● CONNECTING")
        self.status.setObjectName("status")
        header_layout.addWidget(self.status)

        root_layout.addWidget(header_frame)

        # Collapsible Stage 7 System Intelligence Dashboard
        self.system_dashboard = SystemDashboardWidget(parent=self)
        self.system_dashboard.report_to_chat_requested.connect(self.request_system_report)
        root_layout.addWidget(self.system_dashboard)

        # Scrollable Bubble Feed
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("chatScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.messages_container = QWidget()
        self.messages_container.setObjectName("messagesContainer")
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setContentsMargins(12, 12, 12, 12)
        self.messages_layout.setSpacing(10)
        self.messages_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinimumSize
        )

        self.thinking_indicator = ThinkingIndicator(self.messages_container)
        self.messages_layout.addWidget(self.thinking_indicator)
        self.messages_layout.addStretch(1)

        self.scroll_area.setWidget(self.messages_container)
        root_layout.addWidget(self.scroll_area, 1)

        # Connect scrollbar signals for Smart Auto-Scroll
        vbar = self.scroll_area.verticalScrollBar()
        vbar.valueChanged.connect(self._on_scroll_value_changed)
        vbar.rangeChanged.connect(self._on_scroll_range_changed)

        # Initial system message bubble
        self.add_system_message("Sistem başlatılıyor...")

        # Input Bar Card
        input_frame = QFrame(self)
        input_frame.setObjectName("inputFrame")
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(10, 10, 10, 10)
        input_layout.setSpacing(10)

        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("JARVIS'e bir mesaj yaz...")
        self.input.returnPressed.connect(self.send_message)
        self.input.setEnabled(False)

        self.send_button = QPushButton("Gönder")
        self.send_button.setObjectName("sendButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.clicked.connect(self.send_message)
        self.send_button.setEnabled(False)

        input_layout.addWidget(self.input, 1)
        input_layout.addWidget(self.send_button)

        root_layout.addWidget(input_frame)

    # ------------------------------------------------------------------
    # Smart Auto-Scroll Management
    # ------------------------------------------------------------------
    def is_auto_scroll_enabled(self) -> bool:
        return not self._user_scrolled_up

    def _on_scroll_value_changed(self, value: int):
        if self._programmatic_scroll:
            return
        vbar = self.scroll_area.verticalScrollBar()
        distance_to_bottom = vbar.maximum() - value
        self._user_scrolled_up = distance_to_bottom > self.SCROLL_BOTTOM_THRESHOLD

    def _on_scroll_range_changed(self, _min_val: int, max_val: int):
        if not self._user_scrolled_up:
            self._set_scroll_value(max_val)

    def _set_scroll_value(self, target_value: int):
        vbar = self.scroll_area.verticalScrollBar()
        self._programmatic_scroll = True
        try:
            vbar.setValue(target_value)
        finally:
            self._programmatic_scroll = False

    def scroll_to_bottom(self, force: bool = False):
        if force:
            self._user_scrolled_up = False
        if self._user_scrolled_up:
            return
        vbar = self.scroll_area.verticalScrollBar()
        self._set_scroll_value(vbar.maximum())
        QTimer.singleShot(0, self._deferred_scroll_to_bottom)

    def _deferred_scroll_to_bottom(self):
        if not self._user_scrolled_up:
            vbar = self.scroll_area.verticalScrollBar()
            self._set_scroll_value(vbar.maximum())

    # ------------------------------------------------------------------
    # Message Bubble Management
    # ------------------------------------------------------------------
    def _insert_bubble(self, bubble: MessageBubble) -> MessageBubble:
        row = BubbleRow(bubble, self.messages_container)
        # Insert right before the thinking_indicator and bottom stretch
        insert_idx = max(0, self.messages_layout.count() - 2)
        self.messages_layout.insertWidget(insert_idx, row)
        bubble.content_resized.connect(self._on_bubble_resized)
        self._bubbles.append(bubble)
        self.scroll_to_bottom(force=False)
        return bubble

    def _on_bubble_resized(self):
        if not self._user_scrolled_up:
            self.scroll_to_bottom(force=False)

    def add_user_message(self, text: str) -> MessageBubble:
        bubble = MessageBubble(role="user", text=text, parent=self.messages_container)
        # Sending a new user message re-engages auto-scroll to follow the new answer
        self._user_scrolled_up = False
        return self._insert_bubble(bubble)

    def add_assistant_message(self, text: str = "") -> MessageBubble:
        bubble = MessageBubble(role="assistant", text=text, parent=self.messages_container)
        return self._insert_bubble(bubble)

    def add_system_message(self, text: str) -> MessageBubble:
        bubble = MessageBubble(role="system", text=text, parent=self.messages_container)
        return self._insert_bubble(bubble)

    def add_error_message(self, text: str) -> MessageBubble:
        bubble = MessageBubble(role="error", text=text, parent=self.messages_container)
        return self._insert_bubble(bubble)

    def bubbles(self) -> list[MessageBubble]:
        return list(self._bubbles)

    def clear_messages(self):
        self._active_assistant_bubble = None
        self._bubbles.clear()
        # Remove all BubbleRow items (keep thinking_indicator and stretch)
        while self.messages_layout.count() > 2:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._user_scrolled_up = False

    # ------------------------------------------------------------------
    # Status Badge Styling
    # ------------------------------------------------------------------
    def _set_status_badge(self, text: str, state: str):
        self.status.setText(text)
        colors = {
            "connecting": ("#f59e0b", "#2a1f0d", "#78350f"),
            "idle": ("#34d399", "#0d2822", "#065f46"),
            "thinking": ("#c084fc", "#231438", "#581c87"),
            "responding": ("#38bdf8", "#0f243c", "#0369a1"),
            "error": ("#f87171", "#2c1218", "#7f1d1d"),
        }
        fg, bg, border = colors.get(state, colors["idle"])
        self.status.setStyleSheet(
            f"background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 10px; padding: 4px 10px; color: {fg}; "
            f"font-size: 11px; font-weight: bold;"
        )
        self.state_changed.emit(state)

    # ------------------------------------------------------------------
    # Worker Slots & User Interaction
    # ------------------------------------------------------------------
    @Slot(str)
    def update_weather_badge(self, badge_text: str):
        """Update header weather badge button text when worker emits weather_badge_updated."""
        cleaned = (badge_text or "").strip()
        if hasattr(self, "weather_button") and self.weather_button is not None:
            self.weather_button.setText(cleaned if cleaned else "⛅ Hava")

    @Slot()
    def request_weather_report(self):
        """Request a full weather report in chat if ready, otherwise refresh the badge."""
        if self._shutting_down:
            return
        if self.input.isEnabled() and not self.worker.is_busy():
            self.input.setText("/hava")
            self.send_message()
        else:
            self.weather_refresh_requested.emit()

    @Slot()
    def toggle_system_dashboard(self):
        """Toggle visibility of the System Intelligence Dashboard and start/stop
        its background telemetry worker accordingly."""
        if self._shutting_down:
            return
        if not self.system_dashboard.isHidden():
            self.system_dashboard.stop_monitoring()
            self.system_dashboard.hide()
        else:
            self.system_dashboard.show()
            self.system_dashboard.start_monitoring()

    @Slot()
    def request_system_report(self):
        """Send '/sistem' command to generate a full Markdown system health report in chat."""
        if self._shutting_down:
            return
        if self.input.isEnabled() and not self.worker.is_busy():
            self.input.setText("/sistem")
            self.send_message()

    @Slot()
    def on_ready(self):
        self._set_status_badge("● SYSTEM ONLINE", "idle")
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.add_assistant_message("Antigravity bağlantısı kuruldu. Hazırım.")
        self.input.setFocus()
        if self._auto_refresh_weather:
            self.weather_refresh_requested.emit()

    def send_message(self):
        message = self.input.text().strip()
        if not message or self._shutting_down:
            return

        self.add_user_message(message)
        self._active_assistant_bubble = None

        self.input.clear()
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)

        self._set_status_badge("● THINKING", "thinking")
        self.thinking_indicator.start("JARVIS düşünüyor...")
        self.scroll_to_bottom(force=True)

        self.message_requested.emit(message)

    @Slot()
    def _on_thinking_started(self):
        if not self.thinking_indicator.is_active():
            self.thinking_indicator.start("JARVIS düşünüyor...")
        self._set_status_badge("● THINKING", "thinking")

    @Slot(str)
    def _on_worker_state_changed(self, state: str):
        badge_map = {
            "connecting": "● CONNECTING",
            "idle": "● SYSTEM ONLINE",
            "thinking": "● THINKING",
            "responding": "● RESPONDING",
            "error": "● CONNECTION ERROR",
        }
        if state in badge_map:
            self._set_status_badge(badge_map[state], state)

    @Slot(str)
    def on_token(self, token: str):
        if not token:
            return

        if self.thinking_indicator.is_active():
            self.thinking_indicator.stop()

        if self._active_assistant_bubble is None:
            self._active_assistant_bubble = self.add_assistant_message("")
            self._set_status_badge("● RESPONDING", "responding")

        self._active_assistant_bubble.append_token(token)
        self.token_streamed.emit()
        self.scroll_to_bottom(force=False)

    @Slot()
    def on_finished(self):
        self.thinking_indicator.stop()
        if self._active_assistant_bubble is not None:
            self._active_assistant_bubble.finalize()
            self._active_assistant_bubble = None

        self._set_status_badge("● SYSTEM ONLINE", "idle")
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.input.setFocus()
        self.scroll_to_bottom(force=False)

    @Slot(str)
    def on_error(self, message: str):
        self.thinking_indicator.stop()
        if self._active_assistant_bubble is not None:
            self._active_assistant_bubble.finalize()
            self._active_assistant_bubble = None

        self.add_error_message(f"JARVIS ERROR: {message}")
        self._set_status_badge("● CONNECTION ERROR", "error")
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.scroll_to_bottom(force=False)

    # ------------------------------------------------------------------
    # Window Visibility & Non-Blocking Signal-Driven Shutdown
    # ------------------------------------------------------------------
    def hideEvent(self, event):
        if hasattr(self, "system_dashboard") and self.system_dashboard is not None:
            self.system_dashboard.stop_monitoring()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if (
            not self._shutting_down
            and hasattr(self, "system_dashboard")
            and self.system_dashboard is not None
            and not self.system_dashboard.isHidden()
        ):
            self.system_dashboard.start_monitoring()

    def request_shutdown(self):
        """Initiate non-blocking worker shutdown without freezing the main Qt thread."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.thinking_indicator.stop()
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        for btn_attr in ("weather_button", "system_button", "clear_button"):
            btn = getattr(self, btn_attr, None)
            if btn is not None:
                btn.setEnabled(False)

        if hasattr(self, "system_dashboard") and self.system_dashboard is not None:
            self.system_dashboard.request_shutdown()

        if self.verify_threads_stopped(wait_ms=0):
            self._shutdown_complete = True
            self.shutdown_finished.emit()
            return

        if hasattr(self.worker, "request_stop"):
            self.worker.request_stop()

        if self.worker_thread.isRunning():
            self.shutdown_requested.emit()

    def escalate_shutdown(self):
        """Controlled escalation when worker shutdown exceeds its timeout window.
        Forces lingering client subprocesses to abort so the worker thread can unwind."""
        if hasattr(self, "system_dashboard") and self.system_dashboard is not None:
            self.system_dashboard.request_shutdown()
        if hasattr(self.worker, "force_abort_client"):
            self.worker.force_abort_client()
        elif hasattr(self.worker, "request_stop"):
            self.worker.request_stop()
        if self.worker_thread.isRunning() and not self.worker.is_busy():
            self.worker_thread.quit()

    def verify_threads_stopped(self, wait_ms: int = 0) -> bool:
        """Return True only when both worker_thread and monitor_thread have
        genuinely exited, checking the bool return value of QThread.wait()."""
        bound_ms = max(0, int(wait_ms))
        if self.worker_thread.isRunning():
            if not self.worker_thread.wait(bound_ms):
                return False
        if hasattr(self, "system_dashboard") and self.system_dashboard is not None:
            mon_thread = self.system_dashboard.monitor_thread
            if mon_thread.isRunning():
                if not mon_thread.wait(bound_ms):
                    return False
        self._shutdown_complete = True
        return True

    @Slot()
    def _on_worker_shutdown_completed(self):
        if self.worker_thread.isRunning():
            self.worker_thread.quit()

    @Slot()
    def _on_thread_finished(self):
        if self._shutdown_complete:
            return
        if self.verify_threads_stopped(wait_ms=20):
            self._shutdown_complete = True
            self.shutdown_finished.emit()

    def is_shutdown_complete(self) -> bool:
        return self.verify_threads_stopped(wait_ms=0)

    def shutdown(self):
        """Initiate non-blocking shutdown (kept compatible with orb.py aboutToQuit)."""
        self.request_shutdown()
