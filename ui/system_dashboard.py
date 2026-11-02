"""JARVIS Stage 7 — System Intelligence Dashboard & Non-Blocking Telemetry Worker."""

from __future__ import annotations

import threading

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal, Slot, QObject
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.system_service import (
    SystemMonitorService,
    SystemSnapshot,
    fmt_metric,
)


class SparklineGraphWidget(QWidget):
    """Lightweight QPainter 60-second historical sparkline chart.
    Supports 1 primary series and an optional secondary series (for Network RX/TX)."""

    def __init__(
        self,
        line_color: str = "#38bdf8",
        secondary_color: str | None = None,
        fixed_max: float | None = 100.0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._line_color = QColor(line_color)
        self._secondary_color = QColor(secondary_color) if secondary_color else None
        self._fixed_max = fixed_max
        self._primary_data: list[float] = []
        self._secondary_data: list[float] = []

    def set_series(
        self,
        primary: list[float],
        secondary: list[float] | None = None,
    ):
        self._primary_data = list(primary)
        self._secondary_data = list(secondary) if secondary is not None else []
        self.update()

    def series_lengths(self) -> tuple[int, int]:
        return len(self._primary_data), len(self._secondary_data)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        w = rect.width()
        h = rect.height()
        if w <= 4 or h <= 4:
            return

        # Subtle dark graph background & border
        painter.setPen(QPen(QColor(27, 44, 71, 180), 1.0))
        painter.setBrush(QColor(7, 12, 24, 210))
        painter.drawRoundedRect(rect, 6.0, 6.0)

        # Horizontal mid-line grid
        grid_pen = QPen(QColor(30, 49, 80, 110), 1.0, Qt.PenStyle.DotLine)
        painter.setPen(grid_pen)
        painter.drawLine(
            QPointF(rect.left() + 4, rect.center().y()),
            QPointF(rect.right() - 4, rect.center().y()),
        )

        if not self._primary_data and not self._secondary_data:
            return

        # Determine vertical scale
        if self._fixed_max is not None and self._fixed_max > 0:
            max_val = self._fixed_max
        else:
            combined = self._primary_data + self._secondary_data
            max_val = max(combined) if combined else 10.0
            max_val = max(10.0, max_val * 1.15)

        plot_rect = rect.adjusted(4.0, 4.0, -4.0, -4.0)
        self._draw_curve(painter, plot_rect, self._primary_data, max_val, self._line_color, fill=True)
        if self._secondary_color is not None and self._secondary_data:
            self._draw_curve(
                painter,
                plot_rect,
                self._secondary_data,
                max_val,
                self._secondary_color,
                fill=False,
            )

    @staticmethod
    def _draw_curve(
        painter: QPainter,
        plot_rect: QRectF,
        values: list[float],
        max_val: float,
        color: QColor,
        fill: bool,
    ):
        n = len(values)
        if n == 0:
            return

        pw = plot_rect.width()
        ph = plot_rect.height()
        left = plot_rect.left()
        bottom = plot_rect.bottom()

        points: list[QPointF] = []
        if n == 1:
            norm = max(0.0, min(1.0, values[0] / max_val))
            y = bottom - norm * ph
            points = [QPointF(left, y), QPointF(left + pw, y)]
        else:
            step_x = pw / float(max(1, n - 1))
            for idx, val in enumerate(values):
                norm = max(0.0, min(1.0, val / max_val))
                x = left + idx * step_x
                y = bottom - norm * ph
                points.append(QPointF(x, y))

        line_path = QPainterPath()
        line_path.moveTo(points[0])
        for pt in points[1:]:
            line_path.lineTo(pt)

        if fill and len(points) >= 2:
            fill_path = QPainterPath(line_path)
            fill_path.lineTo(QPointF(points[-1].x(), bottom))
            fill_path.lineTo(QPointF(points[0].x(), bottom))
            fill_path.closeSubpath()

            grad = QLinearGradient(0, plot_rect.top(), 0, bottom)
            c_top = QColor(color)
            c_top.setAlpha(85)
            c_bot = QColor(color)
            c_bot.setAlpha(6)
            grad.setColorAt(0.0, c_top)
            grad.setColorAt(1.0, c_bot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawPath(fill_path)

        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(line_path)


class SystemMonitorWorker(QObject):
    """QObject worker living in a background QThread to collect SystemSnapshots
    without ever blocking the main Qt GUI thread."""

    snapshot_ready = Signal(object)

    def __init__(self, service: SystemMonitorService | None = None):
        super().__init__()
        self.service = service if service is not None else SystemMonitorService()
        self._lock = threading.Lock()
        self._active = False
        self._stopping = False

    def is_active(self) -> bool:
        with self._lock:
            return self._active and not self._stopping

    def set_active(self, active: bool):
        with self._lock:
            self._active = bool(active)

    def request_stop(self):
        with self._lock:
            self._active = False
            self._stopping = True

    @Slot()
    def sample_once(self):
        with self._lock:
            if self._stopping:
                return
        try:
            snap = self.service.collect_snapshot(prime_cpu_if_needed=False)
            with self._lock:
                if not self._stopping:
                    self.snapshot_ready.emit(snap)
        except Exception:
            pass


class SystemDashboardWidget(QFrame):
    """Collapsible System Intelligence Dashboard embedded inside ChatWindow.
    Collects metrics strictly on a background QThread while visible, pauses when
    hidden, and terminates cleanly on application shutdown."""

    report_to_chat_requested = Signal()
    sample_requested = Signal()

    def __init__(
        self,
        service: SystemMonitorService | None = None,
        poll_interval_ms: int = 2000,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("systemDashboard")
        self.poll_interval_ms = max(400, poll_interval_ms)
        self.service = service if service is not None else SystemMonitorService()
        self._last_snapshot: SystemSnapshot | None = None
        self._shutting_down = False

        self._apply_styles()
        self._build_ui()

        # Background QThread + Worker setup
        self.monitor_thread = QThread(self)
        self.monitor_worker = SystemMonitorWorker(service=self.service)
        self.monitor_worker.moveToThread(self.monitor_thread)

        self.sample_requested.connect(self.monitor_worker.sample_once)
        self.monitor_worker.snapshot_ready.connect(self._on_snapshot_ready)

        # Periodic timer on main thread that emits queued signal to worker thread ONLY while active
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.poll_interval_ms)
        self.poll_timer.timeout.connect(self._on_poll_tick)

        self.hide()

    def _apply_styles(self):
        self.setStyleSheet("""
            QFrame#systemDashboard {
                background-color: #09101f;
                border: 1px solid #1e3a5f;
                border-radius: 14px;
            }
            QFrame.metricCard {
                background-color: #0d172a;
                border: 1px solid #1b2f4d;
                border-radius: 10px;
            }
            QLabel.cardTitle {
                color: #38bdf8;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 0.7px;
                background: transparent;
            }
            QLabel.cardPrimary {
                color: #f1f5f9;
                font-size: 13px;
                font-weight: bold;
                background: transparent;
            }
            QLabel.cardSecondary {
                color: #94a3b8;
                font-size: 11px;
                background: transparent;
            }
            QProgressBar {
                background-color: #070b14;
                border: 1px solid #1e3352;
                border-radius: 5px;
                height: 8px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #0ea5e9;
                border-radius: 4px;
            }
            QPushButton.dashActionBtn {
                background-color: #13233b;
                border: 1px solid #25426b;
                border-radius: 7px;
                padding: 4px 9px;
                color: #cbd5e1;
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton.dashActionBtn:hover {
                background-color: #1b3355;
                color: #ffffff;
                border-color: #38bdf8;
            }
        """)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # Top Health Banner Row
        banner_row = QHBoxLayout()
        banner_row.setSpacing(8)

        self.health_badge = QLabel("● OPTIMAL")
        self.health_badge.setStyleSheet(
            "background-color:#0d2822; border:1px solid #065f46; border-radius:8px; "
            "padding:3px 8px; color:#34d399; font-size:10.5px; font-weight:bold;"
        )
        banner_row.addWidget(self.health_badge)

        self.health_summary_label = QLabel("Sistem telemetrisi başlatılıyor...")
        self.health_summary_label.setProperty("class", "cardSecondary")
        self.health_summary_label.setWordWrap(True)
        banner_row.addWidget(self.health_summary_label, 1)

        self.updated_label = QLabel("--:--:--")
        self.updated_label.setProperty("class", "cardSecondary")
        banner_row.addWidget(self.updated_label)

        self.refresh_btn = QPushButton("Yenile")
        self.refresh_btn.setProperty("class", "dashActionBtn")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.trigger_immediate_sample)
        banner_row.addWidget(self.refresh_btn)

        self.chat_report_btn = QPushButton("Rapor Yaz")
        self.chat_report_btn.setProperty("class", "dashActionBtn")
        self.chat_report_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_report_btn.clicked.connect(self.report_to_chat_requested.emit)
        banner_row.addWidget(self.chat_report_btn)

        root.addLayout(banner_row)

        # 2x2 Grid for CPU, GPU, RAM/Disk, Network/Battery
        grid = QGridLayout()
        grid.setSpacing(8)

        # --- Card 1: CPU ---
        cpu_card = QFrame(self)
        cpu_card.setProperty("class", "metricCard")
        cpu_layout = QVBoxLayout(cpu_card)
        cpu_layout.setContentsMargins(10, 8, 10, 8)
        cpu_layout.setSpacing(4)

        self.cpu_title = QLabel("CPU • İŞLEMCİ")
        self.cpu_title.setProperty("class", "cardTitle")
        self.cpu_value = QLabel("Ölçülüyor...")
        self.cpu_value.setProperty("class", "cardPrimary")
        self.cpu_sub = QLabel("Sıcaklık: N/A | Frekans: N/A")
        self.cpu_sub.setProperty("class", "cardSecondary")
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setRange(0, 100)
        self.cpu_bar.setValue(0)
        self.cpu_bar.setTextVisible(False)
        self.cpu_graph = SparklineGraphWidget(line_color="#38bdf8", fixed_max=100.0)

        cpu_layout.addWidget(self.cpu_title)
        cpu_layout.addWidget(self.cpu_value)
        cpu_layout.addWidget(self.cpu_sub)
        cpu_layout.addWidget(self.cpu_bar)
        cpu_layout.addWidget(self.cpu_graph)
        grid.addWidget(cpu_card, 0, 0)

        # --- Card 2: GPU ---
        gpu_card = QFrame(self)
        gpu_card.setProperty("class", "metricCard")
        gpu_layout = QVBoxLayout(gpu_card)
        gpu_layout.setContentsMargins(10, 8, 10, 8)
        gpu_layout.setSpacing(4)

        self.gpu_title = QLabel("GPU • EKRAN KARTI")
        self.gpu_title.setProperty("class", "cardTitle")
        self.gpu_value = QLabel("Ölçülüyor...")
        self.gpu_value.setProperty("class", "cardPrimary")
        self.gpu_sub = QLabel("VRAM: N/A | Güç: N/A | Fan: N/A")
        self.gpu_sub.setProperty("class", "cardSecondary")
        self.gpu_bar = QProgressBar()
        self.gpu_bar.setRange(0, 100)
        self.gpu_bar.setValue(0)
        self.gpu_bar.setTextVisible(False)
        self.gpu_graph = SparklineGraphWidget(line_color="#a855f7", fixed_max=100.0)

        gpu_layout.addWidget(self.gpu_title)
        gpu_layout.addWidget(self.gpu_value)
        gpu_layout.addWidget(self.gpu_sub)
        gpu_layout.addWidget(self.gpu_bar)
        gpu_layout.addWidget(self.gpu_graph)
        grid.addWidget(gpu_card, 0, 1)

        # --- Card 3: RAM & Disk ---
        mem_card = QFrame(self)
        mem_card.setProperty("class", "metricCard")
        mem_layout = QVBoxLayout(mem_card)
        mem_layout.setContentsMargins(10, 8, 10, 8)
        mem_layout.setSpacing(4)

        self.mem_title = QLabel("RAM & DİSK")
        self.mem_title.setProperty("class", "cardTitle")
        self.mem_value = QLabel("RAM: -- / -- GB")
        self.mem_value.setProperty("class", "cardPrimary")
        self.disk_sub = QLabel("Disk (/): -- / -- GB")
        self.disk_sub.setProperty("class", "cardSecondary")
        self.mem_bar = QProgressBar()
        self.mem_bar.setRange(0, 100)
        self.mem_bar.setValue(0)
        self.mem_bar.setTextVisible(False)
        self.mem_graph = SparklineGraphWidget(line_color="#34d399", fixed_max=100.0)

        mem_layout.addWidget(self.mem_title)
        mem_layout.addWidget(self.mem_value)
        mem_layout.addWidget(self.disk_sub)
        mem_layout.addWidget(self.mem_bar)
        mem_layout.addWidget(self.mem_graph)
        grid.addWidget(mem_card, 1, 0)

        # --- Card 4: Network & Battery ---
        net_card = QFrame(self)
        net_card.setProperty("class", "metricCard")
        net_layout = QVBoxLayout(net_card)
        net_layout.setContentsMargins(10, 8, 10, 8)
        net_layout.setSpacing(4)

        self.net_title = QLabel("AĞ & BATARYA")
        self.net_title.setProperty("class", "cardTitle")
        self.net_value = QLabel("Ağ: Ölçülüyor...")
        self.net_value.setProperty("class", "cardPrimary")
        self.bat_sub = QLabel("Batarya: N/A")
        self.bat_sub.setProperty("class", "cardSecondary")
        self.net_graph = SparklineGraphWidget(
            line_color="#38bdf8",
            secondary_color="#f59e0b",
            fixed_max=None,
        )

        net_layout.addWidget(self.net_title)
        net_layout.addWidget(self.net_value)
        net_layout.addWidget(self.bat_sub)
        net_layout.addWidget(self.net_graph)
        grid.addWidget(net_card, 1, 1)

        root.addLayout(grid)

        # Footer: Top Processes Summary
        self.proc_label = QLabel("Aktif Süreçler: Ölçülüyor...")
        self.proc_label.setProperty("class", "cardSecondary")
        self.proc_label.setWordWrap(True)
        root.addWidget(self.proc_label)

    # ------------------------------------------------------------------
    # Thread & Monitoring Lifecycle
    # ------------------------------------------------------------------
    def is_monitoring_active(self) -> bool:
        return self.poll_timer.isActive() and self.monitor_worker.is_active()

    def last_snapshot(self) -> SystemSnapshot | None:
        return self._last_snapshot

    @Slot()
    def start_monitoring(self):
        """Start background telemetry collection when Dashboard becomes visible."""
        if self._shutting_down:
            return
        self.monitor_worker.set_active(True)
        if not self.monitor_thread.isRunning():
            self.monitor_thread.start()
        if not self.poll_timer.isActive():
            self.poll_timer.start()
        # Trigger initial sample on worker thread; if CPU/Network are unprimed,
        # schedule a quick second sample 350ms later so real rates appear promptly.
        self.sample_requested.emit()
        QTimer.singleShot(350, self._emit_followup_prime_sample)

    def _emit_followup_prime_sample(self):
        if not self._shutting_down and self.monitor_worker.is_active():
            self.sample_requested.emit()

    @Slot()
    def stop_monitoring(self):
        """Pause background telemetry polling when Dashboard is hidden."""
        self.monitor_worker.set_active(False)
        if self.poll_timer.isActive():
            self.poll_timer.stop()

    @Slot()
    def trigger_immediate_sample(self):
        if not self._shutting_down:
            if not self.monitor_thread.isRunning():
                self.monitor_thread.start()
            self.sample_requested.emit()

    def _on_poll_tick(self):
        if not self._shutting_down and self.monitor_worker.is_active():
            self.sample_requested.emit()

    def request_shutdown(self):
        """Non-blocking shutdown request for the dashboard monitor thread."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.poll_timer.stop()
        self.monitor_worker.request_stop()
        if self.monitor_thread.isRunning():
            self.monitor_thread.quit()

    def shutdown(self) -> bool:
        """Cleanly stop polling and terminate background monitor QThread.
        Returns True when monitor_thread has fully stopped."""
        self.request_shutdown()
        if self.monitor_thread.isRunning():
            return bool(self.monitor_thread.wait(2000))
        return True

    # ------------------------------------------------------------------
    # UI Update Slot
    # ------------------------------------------------------------------
    @Slot(object)
    def _on_snapshot_ready(self, snapshot: SystemSnapshot):
        if self._shutting_down or snapshot is None:
            return
        self._last_snapshot = snapshot
        self.updated_label.setText(f"Son: {snapshot.timestamp}")

        # 1. Health Banner
        h_status = snapshot.health.status
        palette = {
            "OPTIMAL": ("#34d399", "#0d2822", "#065f46"),
            "MODERATE": ("#fbbf24", "#2a1f0d", "#78350f"),
            "HIGH LOAD": ("#fb923c", "#2c160b", "#9a3412"),
            "CRITICAL": ("#f87171", "#2c1218", "#7f1d1d"),
        }
        fg, bg, border = palette.get(h_status, palette["OPTIMAL"])
        self.health_badge.setText(f"● {h_status}")
        self.health_badge.setStyleSheet(
            f"background-color:{bg}; border:1px solid {border}; border-radius:8px; "
            f"padding:3px 8px; color:{fg}; font-size:10.5px; font-weight:bold;"
        )
        self.health_summary_label.setText(snapshot.health.summary)

        # 2. CPU Card
        cpu = snapshot.cpu
        if cpu.is_primed and cpu.usage_percent is not None:
            self.cpu_value.setText(f"%{cpu.usage_percent:.1f}  •  {fmt_metric(cpu.temp_c, '°C', 1)}")
            self.cpu_bar.setValue(int(round(cpu.usage_percent)))
        else:
            self.cpu_value.setText(f"Ölçülüyor...  •  {fmt_metric(cpu.temp_c, '°C', 1)}")
            self.cpu_bar.setValue(0)

        self.cpu_sub.setText(
            f"Frekans: {fmt_metric(cpu.freq_mhz, ' MHz', 0)} | {cpu.core_count} Çekirdek"
        )
        self.cpu_graph.set_series(snapshot.history.cpu_percent)

        # 3. GPU Card
        gpu = snapshot.gpu
        if gpu.available:
            short_gpu = gpu.name.replace("NVIDIA GeForce ", "").replace("Laptop GPU", "Laptop").strip()
            self.gpu_title.setText(f"GPU • {short_gpu[:22]}")
            self.gpu_value.setText(
                f"{fmt_metric(gpu.usage_percent, '%', 0)}  •  {fmt_metric(gpu.temp_c, '°C', 0)}"
            )
            self.gpu_bar.setValue(int(round(gpu.usage_percent or 0.0)))
            vram_txt = (
                f"{fmt_metric(gpu.vram_used_mb, '', 0)}/{fmt_metric(gpu.vram_total_mb, 'MB', 0)}"
                if gpu.vram_used_mb is not None and gpu.vram_total_mb is not None
                else "N/A"
            )
            pwr_txt = (
                f"{fmt_metric(gpu.power_draw_w, 'W', 0)}/{fmt_metric(gpu.power_limit_w, 'W', 0)}"
                if gpu.power_draw_w is not None
                else "N/A"
            )
            fan_txt = fmt_metric(gpu.fan_speed_percent, "%", 0)
            self.gpu_sub.setText(f"VRAM: {vram_txt} | Güç: {pwr_txt} | Fan: {fan_txt}")
        else:
            self.gpu_title.setText("GPU • EKRAN KARTI")
            self.gpu_value.setText("N/A")
            self.gpu_bar.setValue(0)
            self.gpu_sub.setText("VRAM: N/A | Güç: N/A | Fan: N/A")
        self.gpu_graph.set_series(snapshot.history.gpu_percent)

        # 4. RAM & Disk Card
        mem = snapshot.memory
        disk = snapshot.disk
        self.mem_value.setText(f"RAM: {mem.used_gb:.1f} / {mem.total_gb:.1f} GB (%{mem.percent:.0f})")
        self.disk_sub.setText(
            f"Disk ({disk.mount_point}): {disk.used_gb:.0f}/{disk.total_gb:.0f} GB (%{disk.percent:.0f}) • Boş: {disk.free_gb:.0f} GB"
        )
        self.mem_bar.setValue(int(round(mem.percent)))
        self.mem_graph.set_series(snapshot.history.ram_percent)

        # 5. Network & Battery Card
        net = snapshot.network
        if net.is_rate_primed:
            self.net_value.setText(
                f"↓ {fmt_metric(net.rx_rate_kbps, ' KB/s', 1)}  |  ↑ {fmt_metric(net.tx_rate_kbps, ' KB/s', 1)}"
            )
        else:
            self.net_value.setText("Ağ Hızı: Ölçülüyor...")

        bat = snapshot.battery
        if bat.present:
            self.bat_sub.setText(
                f"Batarya ({bat.device_name}): {fmt_metric(bat.percent, '%', 0)} • {bat.status}"
            )
        else:
            self.bat_sub.setText("Batarya: N/A")
        self.net_graph.set_series(
            snapshot.history.net_rx_kbps,
            snapshot.history.net_tx_kbps,
        )

        # 6. Top Processes
        if snapshot.top_processes:
            top_items = [
                f"{p.name} ({p.memory_mb:.0f}MB)" for p in snapshot.top_processes[:4]
            ]
            self.proc_label.setText("Aktif Süreçler: " + " • ".join(top_items))
        else:
            self.proc_label.setText("Aktif Süreçler: N/A")
