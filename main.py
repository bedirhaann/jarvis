#!/usr/bin/env python3
"""JARVIS Desktop Assistant — Main Application Entry Point."""

import os
import signal
import sys

# Ensure project root is on sys.path regardless of current working directory
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.app_context import AppContext
from ui.orb import JarvisOrb


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("JARVIS")
    app.setQuitOnLastWindowClosed(False)
    return app


def main(argv: list[str] | None = None) -> int:
    app = create_application(argv)
    app_context = AppContext.create_default()

    orb = JarvisOrb(start_worker=True, app_context=app_context)
    orb.show()

    # Periodic no-op timer so Python interpreter can process Unix signals while Qt loop runs
    sig_timer = QTimer()
    sig_timer.start(250)
    sig_timer.timeout.connect(lambda: None)

    def _on_about_to_quit():
        """Final safety net when event loop exits. Primary shutdown coordination is
        performed asynchronously inside orb.quit_application() BEFORE app.quit()."""
        sig_timer.stop()
        orb.timer.stop()
        if orb.chat_window.verify_threads_stopped(wait_ms=20):
            return
        orb.chat_window.request_shutdown()
        if not orb.chat_window.verify_threads_stopped(wait_ms=10000):
            orb.chat_window.escalate_shutdown()
            if not orb.chat_window.verify_threads_stopped(wait_ms=2000):
                sys.stderr.write(
                    "JARVIS error: background worker thread did not stop within aboutToQuit window.\n"
                )

    app.aboutToQuit.connect(_on_about_to_quit)

    # Allow Ctrl+C (SIGINT) and SIGTERM in terminal to trigger clean Qt shutdown
    def _handle_unix_signal(_signum, _frame):
        orb.quit_application()

    signal.signal(signal.SIGINT, _handle_unix_signal)
    signal.signal(signal.SIGTERM, _handle_unix_signal)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
