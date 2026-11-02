import os
import subprocess
import sys
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

import main as jarvis_main
from core.antigravity import AntigravityClient
from services.antigravity_worker import AntigravityWorker
from ui.chat import ChatWindow
from ui.orb import JarvisOrb


E2E_STREAM_SCRIPT = r"""
import json, sys, time

print(json.dumps({"event": "init", "conversation_id": "conv-e2e-stage5"}), flush=True)

for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    msg = json.loads(raw_line)
    user_text = msg.get("message", {}).get("content", "")

    chunks = [
        "### Merhaba Bedirhan\n",
        "İşte istediğin **Python** kodu:\n",
        "```python\n",
        "def selamla(isim: str) -> str:\n",
        "    return f'Merhaba {isim}'\n",
        "```\n",
        f"Sorgun (`{user_text}`) başarıyla işlendi.",
    ]
    for ch in chunks:
        print(json.dumps({
            "event": "step_update",
            "step_update": {"text_delta": ch}
        }), flush=True)
        time.sleep(0.03)

    full = "".join(chunks)
    print(json.dumps({
        "event": "result",
        "result": {"status": "SUCCESS", "response": full}
    }), flush=True)
"""


class E2EScriptedClient(AntigravityClient):
    def __init__(self):
        super().__init__(
            model="gemini-4-argon-medium",
            working_dir=".",
            mode="plan",
            grace_timeout=0.3,
            terminate_timeout=0.3,
        )
        self.spawned_processes: list[subprocess.Popen] = []

    def start(self, timeout=10):
        with self._lifecycle_lock:
            if self.is_healthy():
                return self.conversation_id
            if self.process is not None:
                self.close()

            self._closing_event.clear()
            self._cancel_event.clear()
            self._eof_received = False
            self._tainted = False
            self.conversation_id = None
            self.init_event = None
            self.stderr_lines.clear()
            self._drain_events()

            command = [sys.executable, "-u", "-c", E2E_STREAM_SCRIPT]
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self.spawned_processes.append(self.process)

            stdout_thread = threading.Thread(
                target=self._read_stdout,
                args=(self.process.stdout,),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(self.process.stderr,),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            self.reader_threads = [stdout_thread, stderr_thread]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = self.get_event(timeout=0.1)
            if ev is None:
                continue
            if ev.get("event") == "init":
                self.init_event = ev
                self.conversation_id = ev.get("conversation_id")
                return self.conversation_id
        raise TimeoutError("Init timed out")


def wait_for_signal(signal, timeout_ms=4000):
    loop = QEventLoop()
    fired = {"ok": False, "args": None}

    def _on_signal(*args):
        fired["ok"] = True
        fired["args"] = args
        loop.quit()

    signal.connect(_on_signal)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    timer.stop()
    return fired["ok"], fired["args"]


class TestStage5Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = jarvis_main.create_application([])

    def test_full_end_to_end_orb_chat_worker_markdown_and_clean_shutdown(self):
        created_clients: list[E2EScriptedClient] = []

        def factory():
            c = E2EScriptedClient()
            created_clients.append(c)
            return c

        chat = ChatWindow(start_worker=False)
        # Rewire worker with our E2EScriptedClient factory
        chat.worker_thread.started.disconnect(chat.worker.initialize)
        chat.message_requested.disconnect(chat.worker.send_message)
        chat.shutdown_requested.disconnect(chat.worker.shutdown)

        chat.worker = AntigravityWorker(client_factory=factory)
        chat.worker.moveToThread(chat.worker_thread)
        chat.worker_thread.started.connect(chat.worker.initialize)
        chat.message_requested.connect(chat.worker.send_message)
        chat.weather_refresh_requested.connect(chat.worker.refresh_weather_badge)
        chat.worker.ready.connect(chat.on_ready)
        chat.worker.token_received.connect(chat.on_token)
        chat.worker.response_finished.connect(chat.on_finished)
        chat.worker.error_occurred.connect(chat.on_error)
        chat.worker.state_changed.connect(chat._on_worker_state_changed)
        chat.worker.thinking_started.connect(chat._on_thinking_started)
        chat.worker.shutdown_completed.connect(chat._on_worker_shutdown_completed)
        chat.worker.weather_badge_updated.connect(chat.update_weather_badge)
        chat.shutdown_requested.connect(chat.worker.shutdown)

        orb = JarvisOrb(start_worker=False, chat_window=chat)
        orb.show()
        self.app.processEvents()
        self.assertTrue(orb.isVisible())
        self.assertEqual(orb.state(), "connecting")

        observed_orb_states: list[str] = []
        orb.state_updated.connect(observed_orb_states.append)

        # Start worker thread and wait for ready
        chat.worker_thread.start()
        ok_ready, _ = wait_for_signal(chat.worker.ready, timeout_ms=3000)
        self.assertTrue(ok_ready, "Worker did not emit ready")
        self.app.processEvents()

        self.assertEqual(orb.state(), "idle")
        self.assertEqual(chat.status.text(), "● SYSTEM ONLINE")

        # Click Orb to open ChatWindow
        pt = QPoint(60, 60)
        gpt = orb.mapToGlobal(pt)
        orb.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(pt),
                QPointF(gpt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        orb.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(pt),
                QPointF(gpt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        self.app.processEvents()
        self.assertTrue(chat.isVisible())

        # Send a message from ChatWindow
        chat.input.setText("Python selamlama fonksiyonu yaz")
        chat.send_message()

        # Wait for full response to finish streaming
        ok_fin, _ = wait_for_signal(chat.worker.response_finished, timeout_ms=5000)
        self.assertTrue(ok_fin, "Streaming response did not complete")
        self.app.processEvents()

        # Verify state progression included thinking -> responding -> idle
        self.assertIn("thinking", observed_orb_states)
        self.assertIn("responding", observed_orb_states)
        self.assertEqual(orb.state(), "idle")

        # Verify assistant bubble rendered Markdown + code block properly
        last_bubble = chat.bubbles()[-1]
        self.assertEqual(last_bubble.role, "assistant")
        self.assertFalse(last_bubble.is_streaming())
        rendered_html = last_bubble.body.toHtml()
        self.assertIn("Merhaba Bedirhan", rendered_html)
        self.assertIn("PYTHON", rendered_html)
        self.assertIn("def selamla", rendered_html)

        # Trigger application quit via Orb context menu action
        orb.action_quit.trigger()
        if not chat.is_shutdown_complete():
            ok_shut, _ = wait_for_signal(chat.shutdown_finished, timeout_ms=4000)
            self.assertTrue(ok_shut, "Worker shutdown did not finish in time")

        self.assertFalse(chat.worker_thread.isRunning())
        proc = created_clients[0].spawned_processes[0]
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
