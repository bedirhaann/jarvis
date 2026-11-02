import os
import subprocess
import sys
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from core.antigravity import AntigravityCancelledError, AntigravityClient
from services.antigravity_worker import AntigravityWorker
from ui.chat import ChatWindow


class _WorkerSignalBridge(QObject):
    """Main-thread QObject that communicates with AntigravityWorker strictly
    through queued Qt Signal/Slot connections across QThreads."""

    send_requested = Signal(str)
    shutdown_requested = Signal()

    def __init__(self, worker: AntigravityWorker, thread: QThread):
        super().__init__()
        self.worker = worker
        self.thread = thread
        self.send_requested.connect(self.worker.send_message)
        self.shutdown_requested.connect(self.worker.shutdown)

    def request_send(self, text: str):
        self.send_requested.emit(text)

    def request_shutdown(self):
        self.worker.request_stop()
        self.shutdown_requested.emit()


MOCK_AGY_SCRIPT = r"""
import json, sys, time, signal

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

if mode == "ignore_eof":
    signal.signal(signal.SIGINT, signal.SIG_IGN)

print(json.dumps({"event": "init", "conversation_id": f"conv-{mode}"}), flush=True)

for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    msg = json.loads(raw_line)
    text = msg.get("message", {}).get("content", "")

    if mode == "crash":
        sys.stderr.write("Subprocess fatal engine failure\n")
        sys.stderr.flush()
        sys.exit(1)

    elif mode == "hang":
        time.sleep(10)

    elif mode == "slow_stream":
        for i in range(20):
            print(json.dumps({
                "event": "step_update",
                "step_update": {"text_delta": f"parca-{i} "}
            }), flush=True)
            time.sleep(0.08)
        print(json.dumps({
            "event": "result",
            "result": {"status": "SUCCESS", "response": "tamam"}
        }), flush=True)

    elif mode == "ignore_eof":
        print(json.dumps({
            "event": "step_update",
            "step_update": {"text_delta": "basladi "}
        }), flush=True)
        time.sleep(10)

    else:
        print(json.dumps({
            "event": "step_update",
            "step_update": {"text_delta": f"Yanıt: {text}"}
        }), flush=True)
        print(json.dumps({
            "event": "result",
            "result": {"status": "SUCCESS", "response": f"Yanıt: {text}"}
        }), flush=True)

if mode == "ignore_eof":
    time.sleep(10)
"""


class ScriptedAntigravityClient(AntigravityClient):
    """AntigravityClient subclass that spawns python -u -c MOCK_AGY_SCRIPT
    speaking the exact stream-json protocol so real Popen/pipes/threads are tested."""

    def __init__(self, script_mode="normal", grace_timeout=0.3, terminate_timeout=0.3):
        super().__init__(
            model="gemini-4-argon-medium",
            working_dir=".",
            mode="plan",
            grace_timeout=grace_timeout,
            terminate_timeout=terminate_timeout,
        )
        self.script_mode = script_mode
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

            command = [
                sys.executable,
                "-u",
                "-c",
                MOCK_AGY_SCRIPT,
                self.script_mode,
            ]

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
            event = self.get_event(timeout=0.1)
            if event is None:
                continue
            if event.get("event") == "init":
                self.init_event = event
                self.conversation_id = event.get("conversation_id")
                return self.conversation_id
            if event.get("event") == "_eof":
                raise RuntimeError(self._stderr_text())
        raise TimeoutError("Init timed out")


def wait_for_signal(signal, timeout_ms=4000):
    """Run Qt event loop until signal fires or timeout expires (non-blocking loop)."""
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


class TestStage3Threading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_request_close_only_sets_flags_and_never_fakes_eof(self):
        client = ScriptedAntigravityClient(script_mode="normal")
        try:
            client.start()
            proc = client.process
            self.assertIsNotNone(proc)
            self.assertIsNone(proc.poll())

            # request_close() must ONLY set flags and enqueue _control_wakeup
            client.request_close()
            self.assertTrue(client._closing_event.is_set())
            self.assertTrue(client._cancel_event.is_set())

            # Subprocess must still be untouched before worker calls close()
            self.assertIsNone(proc.poll())
            self.assertFalse(proc.stdin.closed)

            ev = client.get_event(timeout=0.2)
            self.assertEqual(ev.get("event"), "_control_wakeup")
            self.assertNotEqual(ev.get("event"), "_eof")
        finally:
            client.close()

    def test_scenario1_close_app_during_active_streaming_does_not_block_main_thread(self):
        created_clients: list[ScriptedAntigravityClient] = []

        def factory():
            c = ScriptedAntigravityClient(script_mode="slow_stream")
            created_clients.append(c)
            return c

        chat = ChatWindow(start_worker=False)
        # Replace worker with one using our streaming factory
        chat.worker_thread.started.disconnect(chat.worker.initialize)
        chat.message_requested.disconnect(chat.worker.send_message)
        chat.shutdown_requested.disconnect(chat.worker.shutdown)

        chat.worker = AntigravityWorker(client_factory=factory)
        chat.worker.moveToThread(chat.worker_thread)
        chat.worker_thread.started.connect(chat.worker.initialize)
        chat.message_requested.connect(chat.worker.send_message)
        chat.worker.ready.connect(chat.on_ready)
        chat.worker.token_received.connect(chat.on_token)
        chat.worker.response_finished.connect(chat.on_finished)
        chat.worker.error_occurred.connect(chat.on_error)
        chat.worker.state_changed.connect(chat._on_worker_state_changed)
        chat.worker.thinking_started.connect(chat._on_thinking_started)
        chat.worker.shutdown_completed.connect(chat._on_worker_shutdown_completed)
        chat.shutdown_requested.connect(chat.worker.shutdown)

        chat.worker_thread.start()
        ok, _ = wait_for_signal(chat.worker.ready, timeout_ms=3000)
        self.assertTrue(ok, "Worker failed to initialize")

        # Start streaming response and wait until first token arrives
        chat.input.setText("Uzun yanıt üret")
        chat.send_message()
        ok_tok, _ = wait_for_signal(chat.worker.token_received, timeout_ms=3000)
        self.assertTrue(ok_tok, "First token never arrived")

        # Now request shutdown on main Qt thread while response is actively streaming!
        t0 = time.monotonic()
        chat.request_shutdown()
        main_thread_elapsed_ms = (time.monotonic() - t0) * 1000.0

        # Main thread call must return almost immediately (< 50 ms) without blocking!
        self.assertLess(main_thread_elapsed_ms, 50.0)

        # Wait via signal for worker_thread.finished
        if not chat.is_shutdown_complete():
            ok_fin, _ = wait_for_signal(chat.shutdown_finished, timeout_ms=4000)
            self.assertTrue(ok_fin, "Shutdown signal did not fire in time")

        self.assertFalse(chat.worker_thread.isRunning())
        proc = created_clients[0].spawned_processes[0]
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        for qthread in chat.worker.executed_qthreads:
            self.assertEqual(qthread, chat.worker_thread)
            self.assertNotEqual(qthread, self.app.thread())
        chat.close()

    def test_scenario2_unexpected_subprocess_crash_emits_real_eof_error(self):
        client = ScriptedAntigravityClient(script_mode="crash")
        thread = QThread()
        worker = AntigravityWorker(client_factory=lambda: client)
        worker.moveToThread(thread)
        bridge = _WorkerSignalBridge(worker, thread)

        thread.started.connect(worker.initialize)
        worker.shutdown_completed.connect(thread.quit)

        thread.start()
        ok, _ = wait_for_signal(worker.ready, timeout_ms=3000)
        self.assertTrue(ok)

        bridge.request_send("Test crash")
        ok_err, err_args = wait_for_signal(worker.error_occurred, timeout_ms=3000)
        self.assertTrue(ok_err, "Expected error_occurred on unexpected subprocess crash")
        error_msg = err_args[0]
        self.assertIn("Antigravity process ended unexpectedly", error_msg)
        self.assertIn("Subprocess fatal engine failure", error_msg)
        self.assertFalse(client.is_healthy())

        bridge.request_shutdown()
        ok_fin, _ = wait_for_signal(thread.finished, timeout_ms=3000)
        self.assertTrue(ok_fin, "Worker QThread did not finish cleanly")
        self.assertFalse(thread.isRunning())

        proc = client.spawned_processes[0]
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        for qthread in worker.executed_qthreads:
            self.assertEqual(qthread, thread)
            self.assertNotEqual(qthread, self.app.thread())

    def test_scenario3_timeout_closes_tainted_process_and_restarts_cleanly(self):
        modes = ["hang", "normal"]
        created_clients: list[ScriptedAntigravityClient] = []

        def factory():
            m = modes.pop(0) if modes else "normal"
            c = ScriptedAntigravityClient(script_mode=m, grace_timeout=0.2, terminate_timeout=0.2)
            created_clients.append(c)
            return c

        thread = QThread()
        worker = AntigravityWorker(client_factory=factory, request_timeout=0.35)
        worker.moveToThread(thread)
        bridge = _WorkerSignalBridge(worker, thread)

        thread.started.connect(worker.initialize)
        worker.shutdown_completed.connect(thread.quit)

        thread.start()
        ok, _ = wait_for_signal(worker.ready, timeout_ms=3000)
        self.assertTrue(ok)
        self.assertEqual(len(created_clients), 1)
        first_proc = created_clients[0].spawned_processes[0]

        # First message hangs and times out (invoked via Signal on worker QThread)
        bridge.request_send("Zaman aşımı testi")
        ok_err, err_args = wait_for_signal(worker.error_occurred, timeout_ms=3000)
        self.assertTrue(ok_err, "Expected TimeoutError signal")
        self.assertIn("timed out", err_args[0])

        # Verify first hung subprocess was closed and marked unhealthy (not reused!)
        self.assertFalse(created_clients[0].is_healthy())
        self.assertIsNotNone(first_proc.poll(), "Timed-out subprocess must be terminated")
        self.assertTrue(first_proc.stdin.closed)
        self.assertTrue(first_proc.stdout.closed)
        self.assertTrue(first_proc.stderr.closed)

        # Send a second message via Signal -> Worker must automatically start a fresh client/subprocess!
        tokens: list[str] = []
        worker.token_received.connect(tokens.append)
        bridge.request_send("Yeni sağlıklı mesaj")
        ok_fin, _ = wait_for_signal(worker.response_finished, timeout_ms=3000)
        self.assertTrue(ok_fin, "Recovery request after timeout failed")

        self.assertEqual(len(created_clients), 2, "Must have created a new client after timeout")
        second_proc = created_clients[1].spawned_processes[0]
        self.assertNotEqual(first_proc.pid, second_proc.pid)
        self.assertIn("Yanıt: Yeni sağlıklı mesaj", "".join(tokens))

        bridge.request_shutdown()
        ok_thread_fin, _ = wait_for_signal(thread.finished, timeout_ms=3000)
        self.assertTrue(ok_thread_fin)
        self.assertFalse(thread.isRunning())
        self.assertIsNotNone(second_proc.poll())
        for qthread in worker.executed_qthreads:
            self.assertEqual(qthread, thread)
            self.assertNotEqual(qthread, self.app.thread())

    def test_scenario4_second_message_during_active_request_is_rejected_safely(self):
        client = ScriptedAntigravityClient(script_mode="slow_stream")
        thread = QThread()
        worker = AntigravityWorker(client_factory=lambda: client)
        worker.moveToThread(thread)
        bridge = _WorkerSignalBridge(worker, thread)

        thread.started.connect(worker.initialize)
        worker.shutdown_completed.connect(thread.quit)

        thread.start()
        ok, _ = wait_for_signal(worker.ready, timeout_ms=3000)
        self.assertTrue(ok)

        # Start first request on worker thread via Signal
        bridge.request_send("Birinci mesaj")
        ok_tok, _ = wait_for_signal(worker.token_received, timeout_ms=3000)
        self.assertTrue(ok_tok)
        self.assertTrue(worker.is_busy())

        # Attempt concurrent second message via Qt Signal while first is still streaming
        bridge.request_send("İkinci çakışan mesaj")
        ok_err, err_args = wait_for_signal(worker.error_occurred, timeout_ms=2000)
        self.assertTrue(ok_err, "Concurrent second message must emit error_occurred")
        self.assertIn("Devam eden bir istek var", err_args[0])
        # First request must still be active
        self.assertTrue(worker.is_busy())

        # Clean up via Signal
        bridge.request_shutdown()
        ok_fin, _ = wait_for_signal(thread.finished, timeout_ms=3000)
        self.assertTrue(ok_fin)
        self.assertFalse(thread.isRunning())
        proc = client.spawned_processes[0]
        self.assertIsNotNone(proc.poll())
        for qthread in worker.executed_qthreads:
            self.assertEqual(qthread, thread)
            self.assertNotEqual(qthread, self.app.thread())

    def test_scenario5_subprocess_genuinely_terminated_after_worker_shutdown(self):
        # Test with a stubborn subprocess that ignores stdin EOF and requires terminate()
        client = ScriptedAntigravityClient(
            script_mode="ignore_eof",
            grace_timeout=0.2,
            terminate_timeout=0.3,
        )
        thread = QThread()
        worker = AntigravityWorker(client_factory=lambda: client)
        worker.moveToThread(thread)
        bridge = _WorkerSignalBridge(worker, thread)

        thread.started.connect(worker.initialize)
        worker.shutdown_completed.connect(thread.quit)

        thread.start()
        ok, _ = wait_for_signal(worker.ready, timeout_ms=3000)
        self.assertTrue(ok)

        proc = client.spawned_processes[0]
        self.assertIsNone(proc.poll())

        bridge.request_send("Baslat")
        ok_tok, _ = wait_for_signal(worker.token_received, timeout_ms=3000)
        self.assertTrue(ok_tok)

        # Trigger shutdown via Signal
        bridge.request_shutdown()
        ok_fin, _ = wait_for_signal(thread.finished, timeout_ms=4000)
        self.assertTrue(ok_fin, "Worker QThread did not finish")

        self.assertFalse(thread.isRunning())
        self.assertIsNotNone(proc.poll(), "Subprocess must be terminated after worker shutdown")
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        for qthread in worker.executed_qthreads:
            self.assertEqual(qthread, thread)
            self.assertNotEqual(qthread, self.app.thread())


if __name__ == "__main__":
    unittest.main(verbosity=2)
