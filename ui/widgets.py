import html
import math
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtGui import (
    QColor,
    QPainter,
    QTextOption,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def _format_content_html(raw_text: str, role: str, is_streaming: bool = False) -> str:
    """Safely format bubble text into HTML (uses markdown_renderer when available)."""
    if role == "assistant":
        try:
            from ui.markdown_renderer import render_markdown
            return render_markdown(raw_text, is_streaming=is_streaming)
        except ImportError:
            pass

    escaped = html.escape(raw_text, quote=True).replace("\n", "<br>")
    color = "#e8f4ff" if role != "error" else "#fecaca"
    return (
        f'<div style="color:{color}; font-size:13.5px; line-height:1.5;">'
        f"{escaped}</div>"
    )


class AutoResizingTextBrowser(QTextBrowser):
    """QTextBrowser that automatically adjusts its height to fit document content
    and forwards wheel events to the parent scroll area."""

    height_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(2)

        self._updating_height = False
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed
        )

        self.document().documentLayout().documentSizeChanged.connect(
            self._update_height
        )

    def _update_height(self, *_args):
        if self._updating_height:
            return
        self._updating_height = True
        try:
            vw = self.viewport().width()
            doc_width = max(200, vw if vw > 50 else 320)
            if abs(self.document().textWidth() - doc_width) > 1:
                self.document().setTextWidth(doc_width)

            doc_height = int(math.ceil(self.document().size().height())) + 8
            doc_height = max(26, doc_height)

            if self.minimumHeight() != doc_height or self.maximumHeight() != doc_height:
                self.setFixedHeight(doc_height)
                self.updateGeometry()
                self.height_changed.emit()
        finally:
            self._updating_height = False

    def showEvent(self, event):
        super().showEvent(event)
        self._update_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def wheelEvent(self, event):
        # Forward vertical scrolling to parent QScrollArea so hovering over a bubble
        # never traps the user's mouse wheel.
        event.ignore()


class ThinkingIndicator(QFrame):
    """Animated neon indicator displayed while Antigravity is thinking/preparing response."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("thinkingIndicator")
        self._phase = 0.0
        self._start_time = 0.0
        self._active = False

        self.setStyleSheet("""
            QFrame#thinkingIndicator {
                background-color: #0e172a;
                border: 1px solid #1e3a5f;
                border-radius: 12px;
            }
            QLabel#thinkingLabel {
                color: #7dd3fc;
                font-size: 12.5px;
                font-weight: 600;
                background: transparent;
            }
            QLabel#thinkingTimer {
                color: #64748b;
                font-size: 11px;
                background: transparent;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        self._dots_canvas = _AnimatedDotsWidget(self)
        layout.addWidget(self._dots_canvas)

        self.label = QLabel("JARVIS düşünüyor...")
        self.label.setObjectName("thinkingLabel")
        layout.addWidget(self.label)

        layout.addStretch()

        self.elapsed_label = QLabel("0.0 sn")
        self.elapsed_label.setObjectName("thinkingTimer")
        layout.addWidget(self.elapsed_label)

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._on_tick)

        self.hide()

    def start(self, text: str = "JARVIS düşünüyor..."):
        self._phase = 0.0
        self._start_time = time.monotonic()
        self._active = True
        self.label.setText(text)
        self.elapsed_label.setText("0.0 sn")
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._active = False
        if self._timer.isActive():
            self._timer.stop()
        self.hide()

    def is_active(self) -> bool:
        return self._active

    def _on_tick(self):
        self._phase += 0.18
        self._dots_canvas.set_phase(self._phase)
        elapsed = max(0.0, time.monotonic() - self._start_time)
        self.elapsed_label.setText(f"{elapsed:.1f} sn")


class _AnimatedDotsWidget(QWidget):
    """Custom painted pulsing wave dots for ThinkingIndicator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 20)
        self._phase = 0.0

    def set_phase(self, phase: float):
        self._phase = phase
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        centers_x = [8, 21, 34]
        center_y = self.height() / 2.0

        for idx, cx in enumerate(centers_x):
            wave = math.sin(self._phase - idx * 0.85)
            offset_y = wave * 3.5
            radius = 3.6 + 0.9 * ((wave + 1.0) / 2.0)
            alpha = int(130 + 125 * ((wave + 1.0) / 2.0))

            glow_color = QColor(56, 189, 248, int(alpha * 0.35))
            painter.setBrush(glow_color)
            painter.drawEllipse(
                int(cx - radius * 1.6),
                int(center_y + offset_y - radius * 1.6),
                int(radius * 3.2),
                int(radius * 3.2),
            )

            dot_color = QColor(125, 211, 252, alpha)
            painter.setBrush(dot_color)
            painter.drawEllipse(
                int(cx - radius),
                int(center_y + offset_y - radius),
                int(radius * 2),
                int(radius * 2),
            )


class MessageBubble(QFrame):
    """Individual chat message bubble for User, Assistant, System, or Error messages."""

    content_resized = Signal()

    ROLE_TITLES = {
        "user": "BEDİRHAN",
        "assistant": "J.A.R.V.I.S.",
        "system": "SİSTEM",
        "error": "SİSTEM HATASI",
    }

    def __init__(self, role: str = "assistant", text: str = "", parent=None):
        super().__init__(parent)
        self.role = role if role in self.ROLE_TITLES else "assistant"
        self._raw_text = ""
        self._is_streaming = False
        self.timestamp = datetime.now().strftime("%H:%M")

        self.setObjectName(f"bubble_{self.role}")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum
        )
        self._apply_role_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 10, 14, 10)
        main_layout.setSpacing(6)

        # Header row: Sender badge + timestamp + optional copy button
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self.sender_label = QLabel(self.ROLE_TITLES[self.role])
        self.sender_label.setObjectName(f"sender_{self.role}")
        header_layout.addWidget(self.sender_label)

        self.time_label = QLabel(self.timestamp)
        self.time_label.setObjectName("timestampLabel")
        header_layout.addWidget(self.time_label)

        header_layout.addStretch()

        self.copy_button = None
        if self.role == "assistant":
            self.copy_button = QPushButton("Kopyala")
            self.copy_button.setObjectName("copyButton")
            self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.copy_button.setFixedHeight(22)
            self.copy_button.clicked.connect(self._copy_to_clipboard)
            header_layout.addWidget(self.copy_button)

        main_layout.addLayout(header_layout)

        # Message body
        self.body = AutoResizingTextBrowser(self)
        self.body.setObjectName("bubbleBody")
        self.body.height_changed.connect(self.content_resized.emit)
        main_layout.addWidget(self.body)

        if text:
            self.set_text(text, is_streaming=False)

    def _apply_role_style(self):
        self.setStyleSheet("""
            QFrame#bubble_user {
                background-color: #132c4e;
                border: 1px solid #255085;
                border-top-left-radius: 14px;
                border-top-right-radius: 4px;
                border-bottom-left-radius: 14px;
                border-bottom-right-radius: 14px;
            }
            QFrame#bubble_assistant {
                background-color: #10192d;
                border: 1px solid #1e3150;
                border-top-left-radius: 4px;
                border-top-right-radius: 14px;
                border-bottom-left-radius: 14px;
                border-bottom-right-radius: 14px;
            }
            QFrame#bubble_system {
                background-color: #0d1b2a;
                border: 1px solid #1b3b5a;
                border-radius: 10px;
            }
            QFrame#bubble_error {
                background-color: #2a121a;
                border: 1px solid #7f1d1d;
                border-radius: 10px;
            }
            QLabel#sender_user {
                color: #7dd3fc;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 0.6px;
                background: transparent;
            }
            QLabel#sender_assistant {
                color: #38bdf8;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 0.8px;
                background: transparent;
            }
            QLabel#sender_system {
                color: #34d399;
                font-size: 11px;
                font-weight: bold;
                background: transparent;
            }
            QLabel#sender_error {
                color: #f87171;
                font-size: 11px;
                font-weight: bold;
                background: transparent;
            }
            QLabel#timestampLabel {
                color: #64748b;
                font-size: 10.5px;
                background: transparent;
            }
            QPushButton#copyButton {
                background-color: #17253d;
                border: 1px solid #294268;
                border-radius: 6px;
                padding: 2px 8px;
                color: #94a3b8;
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton#copyButton:hover {
                background-color: #1e3454;
                color: #e2e8f0;
                border-color: #38bdf8;
            }
            QTextBrowser#bubbleBody {
                background: transparent;
                border: none;
                color: #e8f4ff;
                font-size: 13.5px;
                selection-background-color: #0284c7;
                selection-color: #ffffff;
            }
        """)

    def raw_text(self) -> str:
        return self._raw_text

    def is_streaming(self) -> bool:
        return self._is_streaming

    def set_text(self, text: str, is_streaming: bool = False):
        self._raw_text = text
        self._is_streaming = is_streaming
        self._render()

    def append_token(self, token: str):
        if not token:
            return
        self._raw_text += token
        self._is_streaming = True
        self._render()

    def finalize(self):
        self._is_streaming = False
        self._render()

    def _render(self):
        rendered_html = _format_content_html(
            self._raw_text,
            role=self.role,
            is_streaming=self._is_streaming,
        )
        self.body.setHtml(rendered_html)
        self.body._update_height()

    def _copy_to_clipboard(self):
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._raw_text)
        if self.copy_button:
            self.copy_button.setText("✓ Kopyalandı")
            QTimer.singleShot(
                1500,
                lambda: self.copy_button.setText("Kopyala")
                if self.copy_button
                else None,
            )


class BubbleRow(QWidget):
    """Horizontal container aligning User bubbles right and Assistant bubbles left."""

    def __init__(self, bubble: MessageBubble, parent=None):
        super().__init__(parent)
        self.bubble = bubble
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum
        )

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(2, 2, 2, 2)
        row_layout.setSpacing(0)

        if bubble.role == "user":
            row_layout.addStretch(1)
            row_layout.addWidget(bubble, 5)
        elif bubble.role == "assistant":
            row_layout.addWidget(bubble, 6)
            row_layout.addStretch(1)
        else:
            row_layout.addWidget(bubble, 1)

        bubble.content_resized.connect(self.updateGeometry)

