import json
import queue
import subprocess
import threading
import time
from collections import deque


class AntigravityCancelledError(RuntimeError):
    """Raised when an in-flight Antigravity request is cancelled or shutting down."""


class AntigravityClient:
    def __init__(
        self,
        model="gemini-4-argon-medium",
        working_dir=".",
        mode="plan",
        grace_timeout=5.0,
        terminate_timeout=3.0,
    ):
        self.model = model
        self.working_dir = working_dir
        self.mode = mode
        self.grace_timeout = grace_timeout
        self.terminate_timeout = terminate_timeout

        self.process = None
        self.conversation_id = None

        self.events = queue.Queue()
        self.stderr_lines = deque(maxlen=30)

        self.stdin_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._closing_event = threading.Event()
        self._cancel_event = threading.Event()
        self._eof_received = False
        self._tainted = False

        self.reader_threads = []
        self.init_event = None

    def is_healthy(self) -> bool:
        """Return True only if the subprocess is alive and connection is untainted."""
        with self._lifecycle_lock:
            if self._closing_event.is_set() or self._tainted or self._eof_received:
                return False
            if self.process is None or self.process.poll() is not None:
                return False
            if self.process.stdin is None or self.process.stdin.closed:
                return False
            return True

    def request_cancel(self):
        """Thread-safe non-blocking request cancellation (only sets flags + wakeup)."""
        self._cancel_event.set()
        self.events.put({"event": "_control_wakeup"})

    def request_close(self):
        """Thread-safe non-blocking close signal for main Qt thread.
        Only sets closing/cancel flags and wakes up any waiting get_event() call.
        Actual subprocess cleanup is performed inside close() on the worker thread."""
        self._closing_event.set()
        self._cancel_event.set()
        self.events.put({"event": "_control_wakeup"})

    def _drain_events(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def start(self, timeout=60):
        with self._lifecycle_lock:
            if self.is_healthy():
                return self.conversation_id

            # Ensure any previous broken/stale process is cleaned up before starting
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
                "agy",
                "--model", self.model,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--mode", self.mode
            ]

            self.process = subprocess.Popen(
                command,
                cwd=self.working_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1
            )

            stdout_thread = threading.Thread(
                target=self._read_stdout,
                args=(self.process.stdout,),
                daemon=True
            )

            stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(self.process.stderr,),
                daemon=True
            )

            stdout_thread.start()
            stderr_thread.start()

            self.reader_threads = [stdout_thread, stderr_thread]

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._closing_event.is_set() or self._cancel_event.is_set():
                self._tainted = True
                raise AntigravityCancelledError(
                    "Antigravity başlatma işlemi iptal edildi."
                )

            event = self.get_event(
                timeout=min(0.2, max(0.05, deadline - time.monotonic()))
            )

            if event is None:
                continue

            ev_type = event.get("event")

            if ev_type == "_control_wakeup":
                if self._closing_event.is_set() or self._cancel_event.is_set():
                    self._tainted = True
                    raise AntigravityCancelledError(
                        "Antigravity başlatma işlemi iptal edildi."
                    )
                continue

            if ev_type == "init":
                self.init_event = event
                self.conversation_id = event.get("conversation_id")
                return self.conversation_id

            if ev_type == "_eof":
                self._eof_received = True
                self._tainted = True
                if self._closing_event.is_set():
                    raise AntigravityCancelledError(
                        "Antigravity kapatıldı."
                    )
                raise RuntimeError(self._stderr_text() or "Antigravity başlatılamadı (EOF).")

        self._tainted = True
        raise TimeoutError("Antigravity initialization timed out.")

    def _read_stdout(self, stdout_pipe):
        try:
            for line in stdout_pipe:
                line = line.strip()

                if not line:
                    continue

                try:
                    event = json.loads(line)
                    self.events.put(event)
                except json.JSONDecodeError:
                    self.events.put({
                        "event": "parse_error",
                        "raw": line
                    })

        finally:
            # Real subprocess stdout EOF only — never synthesized by close()
            self.events.put({"event": "_eof"})

    def _read_stderr(self, stderr_pipe):
        try:
            for line in stderr_pipe:
                self.stderr_lines.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def _stderr_text(self):
        return "\n".join(self.stderr_lines)

    def send_message(self, message):
        with self._lifecycle_lock:
            proc = self.process
            if (
                proc is None
                or proc.poll() is not None
                or self._tainted
                or self._closing_event.is_set()
            ):
                raise RuntimeError("Antigravity is not running.")

            payload = {
                "event": "user",
                "message": {
                    "content": message
                }
            }

            encoded = json.dumps(
                payload,
                ensure_ascii=False
            )

            try:
                with self.stdin_lock:
                    proc.stdin.write(encoded + "\n")
                    proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._tainted = True
                raise RuntimeError(f"Antigravity iletişimi koptu: {exc}") from exc

    def get_event(self, timeout=1):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def ask(self, message, on_event=None, timeout=180, on_wait=None):
        self._cancel_event.clear()

        if self._closing_event.is_set():
            raise AntigravityCancelledError("Antigravity kapatılıyor.")

        self.send_message(message)

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if on_wait is not None:
                on_wait()

            if self._closing_event.is_set() or self._cancel_event.is_set():
                self._tainted = True
                raise AntigravityCancelledError("Antigravity isteği iptal edildi.")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            event = self.get_event(timeout=min(0.05, max(0.01, remaining)))

            if event is None:
                # Check if subprocess died silently without emitting result
                with self._lifecycle_lock:
                    proc = self.process
                if proc is not None and proc.poll() is not None and self.events.empty():
                    self._tainted = True
                    if self._closing_event.is_set() or self._cancel_event.is_set():
                        raise AntigravityCancelledError("Antigravity kapatıldı.")
                    stderr_info = self._stderr_text()
                    raise RuntimeError(
                        "Antigravity process ended unexpectedly."
                        + (f" {stderr_info}" if stderr_info else "")
                    )
                continue

            ev_type = event.get("event")

            if ev_type == "_control_wakeup":
                if self._closing_event.is_set() or self._cancel_event.is_set():
                    self._tainted = True
                    raise AntigravityCancelledError("Antigravity isteği iptal edildi.")
                continue

            if ev_type == "_eof":
                self._eof_received = True
                self._tainted = True
                if self._closing_event.is_set() or self._cancel_event.is_set():
                    raise AntigravityCancelledError("Antigravity kapatıldı.")
                stderr_info = self._stderr_text()
                raise RuntimeError(
                    "Antigravity process ended unexpectedly. "
                    + stderr_info
                )

            if on_event:
                on_event(event)

            if ev_type == "result":
                result = event.get("result", {})

                if result.get("status") != "SUCCESS":
                    raise RuntimeError(
                        result.get("error", "Antigravity task failed.")
                    )

                return result.get("response", "")

        # Timeout occurred: mark connection tainted so late events from this timed-out
        # request can never corrupt a future request.
        self._tainted = True
        raise TimeoutError("Antigravity response timed out.")

    def close(self):
        """Perform orderly, staged subprocess cleanup inside the worker thread:
        1. Close stdin and wait up to grace_timeout.
        2. Only on TimeoutExpired: call terminate() and wait up to terminate_timeout.
        3. Only on TimeoutExpired: call kill() and wait."""
        self.request_close()

        with self._lifecycle_lock:
            proc = self.process
            self.process = None
            self.conversation_id = None
            threads = list(self.reader_threads)
            self.reader_threads = []

        if proc is None:
            return

        # Always close stdin first so the subprocess receives EOF
        try:
            with self.stdin_lock:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
        except (OSError, ValueError):
            pass

        if proc.poll() is None:
            try:
                proc.wait(timeout=self.grace_timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                except OSError:
                    pass

                try:
                    proc.wait(timeout=self.terminate_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        else:
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass

        # Close remaining pipes cleanly
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

        cur_thread = threading.current_thread()
        for t in threads:
            if t is not cur_thread and t.is_alive():
                t.join(timeout=0.2)

    def stop(self):
        self.close()
