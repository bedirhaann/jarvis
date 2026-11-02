import os
import threading

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot, QThread

from core.antigravity import AntigravityCancelledError, AntigravityClient
from services.system_service import SystemMonitorService
from services.weather_service import WeatherService


class AntigravityWorker(QObject):
    ready = Signal()
    state_changed = Signal(str)
    thinking_started = Signal()
    token_received = Signal(str)
    response_finished = Signal()
    error_occurred = Signal(str)
    shutdown_completed = Signal()
    weather_badge_updated = Signal(str)

    def __init__(
        self,
        client_factory=None,
        request_timeout: float = 180.0,
        weather_service: WeatherService | None = None,
        system_service: SystemMonitorService | None = None,
    ):
        super().__init__()
        self.client: AntigravityClient | None = None
        self._client_factory = client_factory or self._default_client_factory
        self.request_timeout = request_timeout
        self.weather_service = weather_service if weather_service is not None else WeatherService()
        self.system_service = system_service if system_service is not None else SystemMonitorService()

        self._lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._stopping = threading.Event()
        self._shutdown_done = False
        self._first_token_seen = False
        self.executed_qthreads: list[QThread] = []

    @staticmethod
    def _default_client_factory() -> AntigravityClient:
        return AntigravityClient(
            model="gemini-4-argon-medium",
            working_dir=os.path.expanduser("~/JARVIS"),
            mode="plan",
        )

    def is_busy(self) -> bool:
        with self._busy_lock:
            return self._busy

    def request_stop(self):
        """Thread-safe non-blocking stop request called from the main Qt thread.
        Only sets stop/cancel flags; subprocess cleanup happens on the worker thread."""
        self._stopping.set()
        with self._lock:
            client = self.client
        if client is not None:
            client.request_close()

    def _ensure_healthy_client(self):
        """Ensure client exists and its subprocess is healthy.
        If the previous connection timed out or broke, close it and start a fresh one."""
        with self._lock:
            if self._stopping.is_set():
                raise AntigravityCancelledError("Worker kapatılıyor.")

            if self.client is not None and self.client.is_healthy():
                return self.client

            old_client = self.client
            self.client = None

        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass

        if self._stopping.is_set():
            raise AntigravityCancelledError("Worker kapatılıyor.")

        new_client = self._client_factory()
        with self._lock:
            self.client = new_client

        new_client.start()
        return new_client

    def _record_current_qthread(self):
        cur = QThread.currentThread()
        if cur is not None:
            self.executed_qthreads.append(cur)

    def _pump_worker_events(self):
        if QThread.currentThread() == self.thread():
            QCoreApplication.processEvents()

    @Slot()
    def initialize(self):
        self._record_current_qthread()
        if self._stopping.is_set():
            self._finalize_shutdown()
            return

        self.state_changed.emit("connecting")
        try:
            self._ensure_healthy_client()
            if not self._stopping.is_set():
                self.state_changed.emit("idle")
                self.ready.emit()
        except AntigravityCancelledError:
            self._finalize_shutdown()
        except Exception as error:
            self._cleanup_client()
            if not self._stopping.is_set():
                self.state_changed.emit("error")
                self.error_occurred.emit(str(error))

    @Slot()
    def refresh_weather_badge(self):
        self._record_current_qthread()
        if self._stopping.is_set() or self.weather_service is None:
            return
        try:
            snapshot = self.weather_service.fetch_weather("")
            if not self._stopping.is_set():
                self.weather_badge_updated.emit(snapshot.format_badge())
        except Exception:
            pass

    @Slot(str)
    def send_message(self, message: str):
        self._record_current_qthread()
        if self._stopping.is_set():
            self._finalize_shutdown()
            return

        # Guard against concurrent second message while a request is already active
        with self._busy_lock:
            if self._busy:
                self.error_occurred.emit(
                    "Devam eden bir istek var. Lütfen mevcut yanıtın tamamlanmasını bekleyin."
                )
                return
            self._busy = True

        self._first_token_seen = False
        self.thinking_started.emit()
        self.state_changed.emit("thinking")

        try:
            if self.weather_service is not None and self.weather_service.is_weather_query(message):
                report_md, snapshot = self.weather_service.get_weather_report(message)
                if not self._stopping.is_set():
                    self.weather_badge_updated.emit(snapshot.format_badge())
                    self._first_token_seen = True
                    self.state_changed.emit("responding")
                    self.token_received.emit(report_md)
                    self.state_changed.emit("idle")
                    self.response_finished.emit()
                return

            if self.system_service is not None and self.system_service.is_system_query(message):
                report_md, _sys_snap = self.system_service.get_system_report()
                if not self._stopping.is_set():
                    self._first_token_seen = True
                    self.state_changed.emit("responding")
                    self.token_received.emit(report_md)
                    self.state_changed.emit("idle")
                    self.response_finished.emit()
                return

            client = self._ensure_healthy_client()

            client.ask(
                message,
                on_event=self.handle_event,
                timeout=self.request_timeout,
                on_wait=self._pump_worker_events,
            )

            if not self._stopping.is_set():
                self.state_changed.emit("idle")
                self.response_finished.emit()

        except AntigravityCancelledError:
            # Cancelled due to application shutdown or explicit cancel
            if self._stopping.is_set():
                self._finalize_shutdown()
            else:
                self._cleanup_client()
                self.state_changed.emit("idle")

        except TimeoutError as error:
            # Subprocess timed out: close tainted client immediately so a broken
            # connection is never reused on subsequent requests.
            self._cleanup_client()
            if not self._stopping.is_set():
                self.state_changed.emit("error")
                self.error_occurred.emit(str(error))

        except Exception as error:
            with self._lock:
                client = self.client
            if client is not None and not client.is_healthy():
                self._cleanup_client()

            if not self._stopping.is_set():
                self.state_changed.emit("error")
                self.error_occurred.emit(str(error))

        finally:
            with self._busy_lock:
                self._busy = False
            if self._stopping.is_set():
                self._finalize_shutdown()

    def handle_event(self, event: dict):
        if self._stopping.is_set():
            return

        if event.get("event") == "step_update":
            step = event.get("step_update", {})
            delta = step.get("text_delta")

            if delta:
                if not self._first_token_seen:
                    self._first_token_seen = True
                    self.state_changed.emit("responding")
                self.token_received.emit(delta)

    def _cleanup_client(self):
        with self._lock:
            client = self.client
            self.client = None

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _finalize_shutdown(self):
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self._cleanup_client()
        self.shutdown_completed.emit()

        worker_thread = self.thread()
        app = QCoreApplication.instance()
        main_thread = app.thread() if app is not None else None
        if worker_thread is not None and worker_thread != main_thread:
            worker_thread.quit()

    @Slot()
    def shutdown(self):
        self._record_current_qthread()
        self.request_stop()
        if not self.is_busy():
            self._finalize_shutdown()
