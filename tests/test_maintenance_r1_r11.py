"""Maintenance regression tests (kept separate from Stage 8):

R1  - Asynchronous shutdown state machine:
      * Quitting through JarvisOrb must never block the GUI thread with long
        wait() calls and must never end the Qt event loop while worker_thread
        or monitor_thread is still running.
      * QThread.wait() return values are explicitly checked via
        ChatWindow.verify_threads_stopped().
      * Timeout during shutdown enters a controlled 'timed_out' state, emits
        shutdown_timeout_occurred, escalates subprocess abort without calling
        setParent(None) or app.quit() on a running QThread, and completes only
        after the worker thread genuinely terminates.
      * Duplicate quit requests and repeated callbacks are idempotent.
      * Both `python main.py` and `python ui/orb.py` direct execution paths
        start and shut down cleanly with exit code 0.
R11 - AntigravityWorker.executed_qthreads must stay bounded.
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.antigravity import AntigravityClient  # noqa: E402
from services.antigravity_worker import AntigravityWorker  # noqa: E402
from ui.chat import ChatWindow  # noqa: E402
from ui.orb import JarvisOrb  # noqa: E402


class _SlowClosingClient(AntigravityClient):
    """In-process fake client whose close() takes 1.2s on the worker thread,
    verifying that the GUI event loop stays responsive and waits asynchronously
    for QThread.finished before calling app.quit()."""

    CLOSE_SECONDS = 1.2

    def __init__(self):
        super().__init__()
        self._alive = False
        self.closed_event = threading.Event()

    def start(self, timeout=60):
        self._alive = True
        self.conversation_id = "fake-slow"
        return self.conversation_id

    def is_healthy(self) -> bool:
        return self._alive and not self._closing_event.is_set()

    def close(self):
        self.request_close()
        if self._alive:
            time.sleep(self.CLOSE_SECONDS)
        self._alive = False
        self.closed_event.set()


class _HungUntilEscalatedClient(AntigravityClient):
    """Client whose close() hangs until force_abort() is triggered by timeout
    escalation. Records whether worker_thread was still running when escalation
    occurred."""

    def __init__(self):
        super().__init__()
        self._alive = False
        self.abort_event = threading.Event()
        self.closed_event = threading.Event()
        self.force_abort_called = False

    def start(self, timeout=60):
        self._alive = True
        self.conversation_id = "fake-hung"
        return self.conversation_id

    def is_healthy(self) -> bool:
        return self._alive and not self._closing_event.is_set()

    def force_abort(self):
        self.force_abort_called = True
        # Release the blocked close() after a brief 120ms delay so we can also
        # observe the 'timed_out' state while the thread is still alive.
        threading.Timer(0.12, self.abort_event.set).start()

    def close(self):
        self.request_close()
        if self._alive:
            self.abort_event.wait(timeout=5.0)
        self._alive = False
        self.closed_event.set()


def _wait_until(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
    return predicate()


class TestR1OrbShutdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_quit_waits_for_worker_thread_asynchronously_without_blocking_gui(self):
        chat = ChatWindow(start_worker=False)
        fake = _SlowClosingClient()
        chat.worker._client_factory = lambda: fake
        orb = JarvisOrb(start_worker=False, chat_window=chat)
        orb.show()
        chat.worker_thread.start()
        try:
            self.assertTrue(
                _wait_until(lambda: chat.input.isEnabled(), 5.0),
                "worker never became ready",
            )

            # Count GUI event loop heartbeats during the 1.2s worker shutdown
            # to prove the main thread is NOT blocked by wait().
            gui_ticks = []
            heartbeat = QTimer()
            heartbeat.setInterval(40)
            heartbeat.timeout.connect(lambda: gui_ticks.append(time.monotonic()))
            heartbeat.start()

            QTimer.singleShot(0, orb.quit_application)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(self.app.quit)
            guard.start(10000)

            t0 = time.monotonic()
            self.app.exec()
            elapsed = time.monotonic() - t0
            guard.stop()
            heartbeat.stop()

            self.assertTrue(
                fake.closed_event.is_set(),
                f"event loop ended after {elapsed:.2f}s before client cleanup finished",
            )
            self.assertFalse(chat.worker_thread.isRunning())
            self.assertTrue(chat.verify_threads_stopped(wait_ms=0))
            self.assertEqual(orb.shutdown_state(), "completed")
            self.assertIs(chat.worker_thread.parent(), chat)
            # During a 1.2s non-blocking shutdown, a 40ms GUI timer must tick many times
            self.assertGreaterEqual(
                len(gui_ticks),
                10,
                f"GUI thread was blocked during shutdown (only {len(gui_ticks)} ticks)",
            )
            self.assertLess(elapsed, 6.0)
        finally:
            orb.timer.stop()
            if chat.worker_thread.isRunning():
                chat.worker_thread.wait(5000)
            chat.close()
            orb.close()

    def test_timeout_does_not_call_app_quit_while_thread_running_and_escalates_cleanly(self):
        chat = ChatWindow(start_worker=False)
        hung = _HungUntilEscalatedClient()
        chat.worker._client_factory = lambda: hung
        orb = JarvisOrb(start_worker=False, chat_window=chat)
        orb.shutdown_timeout_ms = 220  # Short timeout to trigger controlled escalation
        orb.show()
        chat.worker_thread.start()
        try:
            self.assertTrue(
                _wait_until(lambda: chat.input.isEnabled(), 5.0),
                "worker never became ready",
            )

            timeout_signals: list[str] = []
            thread_running_at_timeout: list[bool] = []

            def _on_timeout(msg: str):
                timeout_signals.append(msg)
                thread_running_at_timeout.append(chat.worker_thread.isRunning())

            orb.shutdown_timeout_occurred.connect(_on_timeout)

            QTimer.singleShot(0, orb.quit_application)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(self.app.quit)
            guard.start(8000)

            self.app.exec()
            guard.stop()

            # 1. Timeout was detected while worker_thread was still running
            self.assertEqual(len(timeout_signals), 1)
            self.assertEqual(thread_running_at_timeout, [True])
            self.assertIsNotNone(orb.shutdown_error())
            # 2. Escalation invoked force_abort() on the hung client
            self.assertTrue(hung.force_abort_called)
            self.assertTrue(hung.closed_event.is_set())
            # 3. app.exec() only exited AFTER worker_thread genuinely stopped
            self.assertFalse(chat.worker_thread.isRunning())
            self.assertTrue(chat.verify_threads_stopped(wait_ms=0))
            self.assertEqual(orb.shutdown_state(), "completed")
            # 4. QThread was never orphaned via setParent(None)
            self.assertIs(chat.worker_thread.parent(), chat)
        finally:
            hung.abort_event.set()
            orb.timer.stop()
            if chat.worker_thread.isRunning():
                chat.worker_thread.wait(5000)
            chat.close()
            orb.close()

    def test_duplicate_quit_requests_and_callbacks_are_idempotent(self):
        chat = ChatWindow(start_worker=False)
        fake = _SlowClosingClient()
        fake.CLOSE_SECONDS = 0.25
        chat.worker._client_factory = lambda: fake
        orb = JarvisOrb(start_worker=False, chat_window=chat)
        orb.show()
        chat.worker_thread.start()
        try:
            self.assertTrue(_wait_until(lambda: chat.input.isEnabled(), 5.0))

            completed_count = []
            orb.shutdown_completed.connect(lambda: completed_count.append(1))

            def _spam_quit():
                for _ in range(5):
                    orb.quit_application()
                # Also verify UI inputs and actions are immediately disabled
                self.assertFalse(chat.input.isEnabled())
                self.assertFalse(chat.send_button.isEnabled())
                self.assertFalse(orb.action_toggle_chat.isEnabled())
                self.assertFalse(orb.action_quit.isEnabled())

            QTimer.singleShot(0, _spam_quit)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(self.app.quit)
            guard.start(6000)

            self.app.exec()
            guard.stop()

            # Repeated calls after completion must also be no-ops
            orb.quit_application()
            orb._on_chat_shutdown_finished()
            orb._on_shutdown_poll_tick()

            self.assertEqual(len(completed_count), 1)
            self.assertFalse(chat.worker_thread.isRunning())
            self.assertTrue(chat.verify_threads_stopped(wait_ms=0))
        finally:
            orb.timer.stop()
            if chat.worker_thread.isRunning():
                chat.worker_thread.wait(5000)
            chat.close()
            orb.close()

    def test_both_main_py_and_ui_orb_py_direct_execution_clean_shutdown(self):
        """Both `python main.py` and `python ui/orb.py` must start, run the real
        entry point with a stubbed AntigravityClient, handle SIGTERM via
        orb.quit_application(), wait for worker_thread termination, and exit 0
        with zero QThread abort errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Place a sitecustomize.py in tmpdir so subprocesses running main.py
            # or ui/orb.py use a fast local stub instead of spawning live `agy`.
            sitecustom = os.path.join(tmpdir, "sitecustomize.py")
            with open(sitecustom, "w", encoding="utf-8") as f:
                f.write(
                    "import time\n"
                    "import core.antigravity as ag\n"
                    "import services.weather_service as ws\n"
                    "class _StubClient(ag.AntigravityClient):\n"
                    "    def start(self, timeout=60):\n"
                    "        self.conversation_id = 'stub-conv'\n"
                    "        return self.conversation_id\n"
                    "    def is_healthy(self):\n"
                    "        return not self._closing_event.is_set()\n"
                    "    def close(self):\n"
                    "        self.request_close()\n"
                    "        time.sleep(0.15)\n"
                    "ag.AntigravityClient = _StubClient\n"
                    "ws.WeatherService.fetch_weather = lambda self, q='': (_ for _ in ()).throw(RuntimeError('offline'))\n"
                )

            env = {
                **os.environ,
                "QT_QPA_PLATFORM": "offscreen",
                "PYTHONPATH": f"{tmpdir}:{PROJECT_ROOT}",
                "JARVIS_DATA_DIR": os.path.join(tmpdir, "jarvis_data"),
            }

            for entry_script in (
                os.path.join(PROJECT_ROOT, "main.py"),
                os.path.join(PROJECT_ROOT, "ui", "orb.py"),
            ):
                proc = subprocess.Popen(
                    [sys.executable, entry_script],
                    cwd="/",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                try:
                    # Allow app and worker thread to initialize, then send SIGTERM
                    time.sleep(0.7)
                    self.assertIsNone(
                        proc.poll(),
                        f"{entry_script} exited prematurely before SIGTERM",
                    )
                    proc.send_signal(signal.SIGTERM)
                    stdout, stderr = proc.communicate(timeout=8.0)
                    self.assertEqual(
                        proc.returncode,
                        0,
                        f"{entry_script} exited with {proc.returncode}: {stderr}",
                    )
                    self.assertNotIn("Destroyed while thread", stderr)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=2.0)


class TestR11BoundedThreadHistory(unittest.TestCase):
    def test_executed_qthreads_is_bounded(self):
        worker = AntigravityWorker(client_factory=lambda: None)
        for _ in range(5000):
            worker._record_current_qthread()
        self.assertLessEqual(
            len(worker.executed_qthreads), AntigravityWorker.QTHREAD_HISTORY_LIMIT
        )
        self.assertGreater(len(worker.executed_qthreads), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
