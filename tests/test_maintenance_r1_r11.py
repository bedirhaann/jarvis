"""Maintenance regression tests (kept separate from Stage 8):

R1  - Quitting through JarvisOrb must never end the Qt event loop while the
      worker QThread is still cleaning up the agy subprocess, regardless of the
      entry point (main.py, `python -m ui.orb`, `python ui/orb.py`).
R11 - AntigravityWorker.executed_qthreads must stay bounded.
"""

import os
import subprocess
import sys
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
    """In-process fake client whose close() takes longer than the orb's 4 s
    fallback timer, emulating a real agy subprocess that needs the full
    stdin-EOF -> terminate grace period."""

    CLOSE_SECONDS = 4.8

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

    def test_quit_waits_for_worker_thread_even_when_cleanup_exceeds_fallback(self):
        chat = ChatWindow(start_worker=False)
        fake = _SlowClosingClient()
        chat.worker._client_factory = lambda: fake
        orb = JarvisOrb(start_worker=False, chat_window=chat)
        orb.show()
        chat.worker_thread.start()
        try:
            self.assertTrue(_wait_until(lambda: chat.input.isEnabled(), 5.0),
                            "worker never became ready")

            # Run the real QApplication event loop; app.quit() (from the orb) exits it.
            QTimer.singleShot(0, orb.quit_application)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(self.app.quit)
            guard.start(15000)
            t0 = time.monotonic()
            self.app.exec()
            elapsed = time.monotonic() - t0
            guard.stop()

            # The loop must not have been released while the worker was still
            # cleaning up the client (the original R1 abort condition).
            self.assertTrue(fake.closed_event.is_set(),
                            f"event loop ended after {elapsed:.2f}s before client cleanup finished")
            self.assertTrue(chat.worker_thread.wait(2000))
            self.assertFalse(chat.worker_thread.isRunning())
            self.assertLess(elapsed, 14.0)
        finally:
            orb.timer.stop()
            if chat.worker_thread.isRunning():
                chat.worker_thread.wait(10000)
            chat.close()
            orb.close()

    def test_orb_file_entry_point_bootstraps_path_and_delegates_to_main(self):
        """`python ui/orb.py` (run from any cwd) must resolve the project package
        and delegate to main.main(), which owns the safe shutdown sequence.
        main.main is replaced by a stub so no window/worker is started."""
        orb_path = os.path.join(PROJECT_ROOT, "ui", "orb.py")
        script = (
            "import sys, types, runpy\n"
            "calls = []\n"
            "fake = types.ModuleType('main')\n"
            "fake.main = lambda argv=None: calls.append(argv) or 0\n"
            "sys.modules['main'] = fake\n"
            f"sys.argv = [{orb_path!r}]\n"
            "try:\n"
            f"    runpy.run_path({orb_path!r}, run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    assert exc.code == 0, exc.code\n"
            "assert calls, 'main.main() was not called'\n"
            "print('delegated-ok')\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["QT_QPA_PLATFORM"] = "offscreen"
        direct = subprocess.run(
            [sys.executable, "-c", script], cwd="/", capture_output=True, text=True,
            timeout=60, env=env,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn("delegated-ok", direct.stdout)


class TestR11BoundedThreadHistory(unittest.TestCase):
    def test_executed_qthreads_is_bounded(self):
        worker = AntigravityWorker(client_factory=lambda: None)
        for _ in range(5000):
            worker._record_current_qthread()
        self.assertLessEqual(len(worker.executed_qthreads), AntigravityWorker.QTHREAD_HISTORY_LIMIT)
        self.assertGreater(len(worker.executed_qthreads), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
