import os
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot, QThread

from core.antigravity import AntigravityCancelledError, AntigravityClient
from services.system_service import SystemMonitorService
from services.weather_service import WeatherService

if TYPE_CHECKING:
    from services.memory.service import MemoryService


class AntigravityWorker(QObject):
    QTHREAD_HISTORY_LIMIT = 64

    ready = Signal()
    state_changed = Signal(str)
    thinking_started = Signal()
    token_received = Signal(str)
    response_finished = Signal()
    error_occurred = Signal(str)
    shutdown_completed = Signal()
    weather_badge_updated = Signal(str)
    memory_badge_updated = Signal(str)
    conversation_reset_completed = Signal(int)

    def __init__(
        self,
        client_factory=None,
        request_timeout: float = 180.0,
        weather_service: WeatherService | None = None,
        system_service: SystemMonitorService | None = None,
        memory_service: "MemoryService | None" = None,
        memory_service_factory: "Callable[[], MemoryService] | None" = None,
    ):
        super().__init__()
        self.client: AntigravityClient | None = None
        self._client_factory = client_factory or self._default_client_factory
        self.request_timeout = request_timeout
        self.weather_service = weather_service if weather_service is not None else WeatherService()
        self.system_service = system_service if system_service is not None else SystemMonitorService()
        self.memory_service: "MemoryService | None" = memory_service
        self._memory_service_factory = memory_service_factory

        self._lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._stopping = threading.Event()
        self._shutdown_done = False
        self._first_token_seen = False
        self._response_buffer: list[str] = []
        self._closing_client: AntigravityClient | None = None
        self.executed_qthreads: deque[QThread] = deque(maxlen=self.QTHREAD_HISTORY_LIMIT)

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
            client = self.client or self._closing_client
        if client is not None:
            client.request_close()

    def force_abort_client(self):
        """Emergency non-blocking escalation called if worker shutdown exceeds the
        staged timeout window. Signals force_abort/SIGKILL on any lingering client
        subprocess so the blocked worker thread can unwind and exit cleanly."""
        self._stopping.set()
        with self._lock:
            client = self.client or self._closing_client
        if client is None:
            return
        try:
            client.request_close()
        except Exception:
            pass
        if hasattr(client, "force_abort") and callable(client.force_abort):
            try:
                client.force_abort()
            except Exception:
                pass
        proc = getattr(client, "process", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _ensure_memory_initialized(self):
        if self.memory_service is None and self._memory_service_factory is not None:
            self.memory_service = self._memory_service_factory()
        if self.memory_service is not None and not self.memory_service.is_initialized:
            self.memory_service.initialize()
            self.memory_badge_updated.emit(self.memory_service.status_badge_text())

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
            if self.memory_service is not None and self.memory_service.is_initialized:
                try:
                    self.memory_service.on_session_ended(end_reason="restarted")
                except Exception:
                    pass

        if self._stopping.is_set():
            raise AntigravityCancelledError("Worker kapatılıyor.")

        new_client = self._client_factory()
        with self._lock:
            self.client = new_client

        new_client.start()
        if self.memory_service is not None and self.memory_service.is_initialized:
            try:
                self.memory_service.on_session_started(
                    backend_conv_id=getattr(new_client, "conversation_id", None),
                    model=getattr(new_client, "model", None),
                    mode=getattr(new_client, "mode", None),
                )
            except Exception:
                pass
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
            self._ensure_memory_initialized()
            self._ensure_healthy_client()
            if not self._stopping.is_set():
                self.state_changed.emit("idle")
                self.ready.emit()
        except AntigravityCancelledError:
            self._finalize_shutdown()
        except Exception as error:
            self._cleanup_client(end_reason="error")
            if not self._stopping.is_set():
                self.state_changed.emit("error")
                self.error_occurred.emit(str(error))
        finally:
            if self._stopping.is_set() and not self.is_busy():
                self._finalize_shutdown()

    @Slot()
    def reset_conversation(self):
        """Start a fresh conversation and fresh agy session when 'Temizle' is clicked.
        Preserves global and project memories while discarding previous conversation context."""
        self._record_current_qthread()
        if self._stopping.is_set():
            if not self.is_busy():
                self._finalize_shutdown()
            return

        try:
            self._ensure_memory_initialized()
            self._cleanup_client(end_reason="reset")
            if self.memory_service is not None:
                new_conv = self.memory_service.start_new_conversation()
                self.memory_badge_updated.emit(self.memory_service.status_badge_text())
                if not self._stopping.is_set():
                    self.conversation_reset_completed.emit(new_conv.id)
            else:
                if not self._stopping.is_set():
                    self.conversation_reset_completed.emit(0)
        except Exception as error:
            if not self._stopping.is_set():
                self.error_occurred.emit(str(error))
        finally:
            if self._stopping.is_set() and not self.is_busy():
                self._finalize_shutdown()

    @Slot()
    def refresh_weather_badge(self):
        self._record_current_qthread()
        if self._stopping.is_set() or self.weather_service is None:
            if self._stopping.is_set() and not self.is_busy():
                self._finalize_shutdown()
            return
        try:
            snapshot = self.weather_service.fetch_weather("")
            if not self._stopping.is_set():
                self.weather_badge_updated.emit(snapshot.format_badge())
        except Exception:
            pass
        finally:
            if self._stopping.is_set() and not self.is_busy():
                self._finalize_shutdown()

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
        self._response_buffer = []
        self.thinking_started.emit()
        self.state_changed.emit("thinking")

        try:
            self._ensure_memory_initialized()

            # Route 0: Local memory commands (/hatırla, /unut, /hafıza, /proje, /geçmiş)
            if self.memory_service is not None and self.memory_service.is_memory_command(message):
                handled, report_md = self.memory_service.handle_command(message)
                if handled:
                    if not self._stopping.is_set():
                        self.memory_badge_updated.emit(self.memory_service.status_badge_text())
                        self._first_token_seen = True
                        self.state_changed.emit("responding")
                        self.token_received.emit(report_md)
                        self.state_changed.emit("idle")
                        self.response_finished.emit()
                    return

            # Route 1: Weather service
            if self.weather_service is not None and self.weather_service.is_weather_query(message):
                t0 = time.monotonic()
                report_md, snapshot = self.weather_service.get_weather_report(message)
                latency_ms = int((time.monotonic() - t0) * 1000)
                if self.memory_service is not None:
                    self.memory_service.record_local_turn(
                        message, report_md, source="local:weather", latency_ms=latency_ms
                    )
                if not self._stopping.is_set():
                    self.weather_badge_updated.emit(snapshot.format_badge())
                    self._first_token_seen = True
                    self.state_changed.emit("responding")
                    self.token_received.emit(report_md)
                    self.state_changed.emit("idle")
                    self.response_finished.emit()
                return

            # Route 2: System monitor service
            if self.system_service is not None and self.system_service.is_system_query(message):
                t0 = time.monotonic()
                report_md, _sys_snap = self.system_service.get_system_report()
                latency_ms = int((time.monotonic() - t0) * 1000)
                if self.memory_service is not None:
                    self.memory_service.record_local_turn(
                        message, report_md, source="local:system", latency_ms=latency_ms
                    )
                if not self._stopping.is_set():
                    self._first_token_seen = True
                    self.state_changed.emit("responding")
                    self.token_received.emit(report_md)
                    self.state_changed.emit("idle")
                    self.response_finished.emit()
                return

            # Route 3: Antigravity LLM with local memory bootstrap/retrieval
            t0 = time.monotonic()
            client = self._ensure_healthy_client()

            prompt_for_llm = message
            pending_suggestions = []
            if self.memory_service is not None:
                _user_msg, prompt_for_llm, pending_suggestions = (
                    self.memory_service.prepare_user_turn(message)
                )

            client.ask(
                prompt_for_llm,
                on_event=self.handle_event,
                timeout=self.request_timeout,
                on_wait=self._pump_worker_events,
            )

            if self.memory_service is not None:
                self.memory_service.update_session_backend_id(
                    getattr(client, "conversation_id", None)
                )

            if pending_suggestions and not self._stopping.is_set() and self.memory_service is not None:
                footer = self.memory_service.format_pending_suggestions_notice(pending_suggestions)
                if footer:
                    if not self._first_token_seen:
                        self._first_token_seen = True
                        self.state_changed.emit("responding")
                    self._response_buffer.append(footer)
                    self.token_received.emit(footer)

            if self.memory_service is not None:
                latency_ms = int((time.monotonic() - t0) * 1000)
                full_response = "".join(self._response_buffer)
                self.memory_service.record_assistant_turn(
                    full_response,
                    source="llm",
                    status="complete",
                    latency_ms=latency_ms,
                )
                self.memory_badge_updated.emit(self.memory_service.status_badge_text())

            if not self._stopping.is_set():
                self.state_changed.emit("idle")
                self.response_finished.emit()

        except AntigravityCancelledError:
            if self.memory_service is not None and self._response_buffer:
                try:
                    self.memory_service.record_assistant_turn(
                        "".join(self._response_buffer),
                        source="llm",
                        status="cancelled",
                    )
                except Exception:
                    pass
            # Cancelled due to application shutdown or explicit cancel
            if self._stopping.is_set():
                self._finalize_shutdown()
            else:
                self._cleanup_client(end_reason="cancelled")
                self.state_changed.emit("idle")

        except TimeoutError as error:
            if self.memory_service is not None and self._response_buffer:
                try:
                    self.memory_service.record_assistant_turn(
                        "".join(self._response_buffer),
                        source="llm",
                        status="partial",
                    )
                except Exception:
                    pass
            # Subprocess timed out: close tainted client immediately so a broken
            # connection is never reused on subsequent requests.
            self._cleanup_client(end_reason="timeout")
            if not self._stopping.is_set():
                self.state_changed.emit("error")
                self.error_occurred.emit(str(error))

        except Exception as error:
            if self.memory_service is not None and self._response_buffer:
                try:
                    self.memory_service.record_assistant_turn(
                        "".join(self._response_buffer),
                        source="llm",
                        status="partial",
                    )
                except Exception:
                    pass
            with self._lock:
                client = self.client
            if client is not None and not client.is_healthy():
                self._cleanup_client(end_reason="error")

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
                self._response_buffer.append(delta)
                self.token_received.emit(delta)

    def _cleanup_client(self, end_reason: str = "closed"):
        with self._lock:
            client = self.client
            self.client = None
            self._closing_client = client

        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            finally:
                with self._lock:
                    if self._closing_client is client:
                        self._closing_client = None

        if self.memory_service is not None and self.memory_service.is_initialized:
            try:
                self.memory_service.on_session_ended(end_reason=end_reason)
            except Exception:
                pass

    def _finalize_shutdown(self):
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self._cleanup_client(end_reason="shutdown")
        if self.memory_service is not None:
            try:
                self.memory_service.close()
            except Exception:
                pass
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
