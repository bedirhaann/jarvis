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

    orb = JarvisOrb(start_worker=True)
    orb.show()

    def _on_about_to_quit():
        orb.timer.stop()
        orb.chat_window.request_shutdown()
        if orb.chat_window.worker_thread.isRunning():
            orb.chat_window.worker_thread.wait(4000)

    app.aboutToQuit.connect(_on_about_to_quit)

    # Allow Ctrl+C (SIGINT) and SIGTERM in terminal to trigger clean Qt shutdown
    def _handle_unix_signal(_signum, _frame):
        orb.quit_application()

    signal.signal(signal.SIGINT, _handle_unix_signal)
    signal.signal(signal.SIGTERM, _handle_unix_signal)

    # Periodic no-op timer so Python interpreter can process Unix signals while Qt loop runs
    sig_timer = QTimer()
    sig_timer.start(250)
    sig_timer.timeout.connect(lambda: None)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
