import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from services.system_service import (
    CpuMetrics,
    DiskMetrics,
    GpuMetrics,
    MemoryMetrics,
    ProcessItem,
    SystemMonitorService,
)
from ui.chat import ChatWindow
from ui.system_dashboard import SparklineGraphWidget


def _get_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _pump_events(ms: int = 80):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class TestStage7SystemIntelligence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _get_app()

    def test_1_cpu_temperature_verified_sensor_filtering(self):
        """Unverified thermal zones (acpitz, INT3400) must be rejected; only verified CPU sensors are used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sysfs = Path(tmpdir)
            hwmon_base = sysfs / "class" / "hwmon"
            thermal_base = sysfs / "class" / "thermal"
            hwmon_base.mkdir(parents=True)
            thermal_base.mkdir(parents=True)

            # Case A: Only unverified acpitz & INT3400 Thermal & nvme -> must return (None, None)
            hw0 = hwmon_base / "hwmon0"
            hw0.mkdir()
            (hw0 / "name").write_text("acpitz\n", encoding="utf-8")
            (hw0 / "temp1_input").write_text("27800\n", encoding="utf-8")

            hw1 = hwmon_base / "hwmon1"
            hw1.mkdir()
            (hw1 / "name").write_text("nvme\n", encoding="utf-8")
            (hw1 / "temp1_input").write_text("39800\n", encoding="utf-8")

            tz0 = thermal_base / "thermal_zone0"
            tz0.mkdir()
            (tz0 / "type").write_text("acpitz\n", encoding="utf-8")
            (tz0 / "temp").write_text("27800\n", encoding="utf-8")

            tz1 = thermal_base / "thermal_zone1"
            tz1.mkdir()
            (tz1 / "type").write_text("INT3400 Thermal\n", encoding="utf-8")
            (tz1 / "temp").write_text("20000\n", encoding="utf-8")

            svc = SystemMonitorService(sysfs_root=sysfs)
            temp_c, source = svc._read_verified_cpu_temperature()
            self.assertIsNone(temp_c)
            self.assertIsNone(source)

            # Case B: Verified thermal_zone5 'x86_pkg_temp' -> accepted when hwmon has no CPU driver
            tz5 = thermal_base / "thermal_zone5"
            tz5.mkdir()
            (tz5 / "type").write_text("x86_pkg_temp\n", encoding="utf-8")
            (tz5 / "temp").write_text("54000\n", encoding="utf-8")

            temp_c, source = svc._read_verified_cpu_temperature()
            self.assertAlmostEqual(temp_c, 54.0, places=1)
            self.assertEqual(source, "thermal:x86_pkg_temp")

            # Case C: Verified hwmon 'coretemp' with Package id 0 -> takes priority
            hw7 = hwmon_base / "hwmon7"
            hw7.mkdir()
            (hw7 / "name").write_text("coretemp\n", encoding="utf-8")
            (hw7 / "temp1_input").write_text("51500\n", encoding="utf-8")
            (hw7 / "temp1_label").write_text("Package id 0\n", encoding="utf-8")
            (hw7 / "temp2_input").write_text("48000\n", encoding="utf-8")
            (hw7 / "temp2_label").write_text("Core 0\n", encoding="utf-8")

            temp_c, source = svc._read_verified_cpu_temperature()
            self.assertAlmostEqual(temp_c, 51.5, places=1)
            self.assertIn("coretemp:package id 0", source)

    def test_2_dynamic_battery_and_ac_discovery(self):
        """Battery discovery must dynamically scan /sys/class/power_supply/ without hardcoding BAT1."""
        # Case A: BAT0 + AC adapter present
        with tempfile.TemporaryDirectory() as tmpdir:
            sysfs = Path(tmpdir)
            ps_base = sysfs / "class" / "power_supply"
            ps_base.mkdir(parents=True)

            bat0 = ps_base / "BAT0"
            bat0.mkdir()
            (bat0 / "type").write_text("Battery\n", encoding="utf-8")
            (bat0 / "capacity").write_text("84\n", encoding="utf-8")
            (bat0 / "status").write_text("Charging\n", encoding="utf-8")

            ac = ps_base / "AC"
            ac.mkdir()
            (ac / "type").write_text("Mains\n", encoding="utf-8")
            (ac / "online").write_text("1\n", encoding="utf-8")

            svc = SystemMonitorService(sysfs_root=sysfs)
            bat = svc._collect_battery_metrics()
            self.assertTrue(bat.present)
            self.assertEqual(bat.device_name, "BAT0")
            self.assertEqual(bat.adapter_name, "AC")
            self.assertAlmostEqual(bat.percent, 84.0)
            self.assertTrue(bat.is_plugged)
            self.assertEqual(bat.status, "Şarj Oluyor")

        # Case B: Desktop PC without any battery -> graceful N/A (present=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            sysfs = Path(tmpdir)
            ps_base = sysfs / "class" / "power_supply"
            ps_base.mkdir(parents=True)

            acad = ps_base / "ACAD"
            acad.mkdir()
            (acad / "type").write_text("Mains\n", encoding="utf-8")
            (acad / "online").write_text("1\n", encoding="utf-8")

            svc = SystemMonitorService(sysfs_root=sysfs)
            bat = svc._collect_battery_metrics()
            self.assertFalse(bat.present)
            self.assertIsNone(bat.percent)
            self.assertEqual(bat.status, "Batarya Yok")

    def test_3_nvidia_rtx4060_metrics_and_na_fallback(self):
        """RTX 4060 GPU query parses utilization, temp, VRAM, power draw, enforced power limit fallback, and [N/A] fan speed."""
        captured_calls = []

        def fake_runner(args: list[str], timeout: float) -> str | None:
            captured_calls.append((args, timeout))
            return (
                "NVIDIA GeForce RTX 4060 Laptop GPU, 19, 46, 875, 8188, "
                "12.45, [N/A], 45.00, 80.00, [N/A]\n"
            )

        svc = SystemMonitorService(command_runner=fake_runner)
        gpu = svc._collect_gpu_metrics()

        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0][0][0], "nvidia-smi")
        self.assertTrue(gpu.available)
        self.assertEqual(gpu.name, "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertAlmostEqual(gpu.usage_percent, 19.0)
        self.assertAlmostEqual(gpu.temp_c, 46.0)
        self.assertAlmostEqual(gpu.vram_used_mb, 875.0)
        self.assertAlmostEqual(gpu.vram_total_mb, 8188.0)
        self.assertAlmostEqual(gpu.power_draw_w, 12.45)
        # power.limit was [N/A], so it fell back to enforced.power.limit = 45.00 W
        self.assertAlmostEqual(gpu.power_limit_w, 45.00)
        # fan.speed was [N/A] on laptop GPU -> None
        self.assertIsNone(gpu.fan_speed_percent)

    def test_4_sampling_accuracy_cpu_priming_and_network_delta(self):
        """First CPU sample is discarded (is_primed=False); network rates require 2 valid monotonic samples and reject negative deltas."""
        svc = SystemMonitorService()
        self.assertFalse(svc._cpu_primed)
        cpu1 = svc._collect_cpu_metrics(prime_if_needed=False)
        self.assertFalse(cpu1.is_primed)
        self.assertIsNone(cpu1.usage_percent)

        cpu2 = svc._collect_cpu_metrics(prime_if_needed=False)
        self.assertTrue(cpu2.is_primed)
        self.assertIsNotNone(cpu2.usage_percent)

        # Network delta verification (using instance-scoped monotonic clock, never patching global time.monotonic)
        fake_timestamps = iter([100.0, 102.0, 104.0])
        svc_net = SystemMonitorService(monotonic_clock=lambda: next(fake_timestamps))
        fake_stats = {"wlan0": SimpleNamespace(isup=True)}
        c1 = {"wlan0": SimpleNamespace(bytes_recv=100_000, bytes_sent=50_000)}
        c2 = {"wlan0": SimpleNamespace(bytes_recv=151_200, bytes_sent=75_600)}  # +50 KB RX, +25 KB TX
        c3_reset = {"wlan0": SimpleNamespace(bytes_recv=10_000, bytes_sent=5_000)}  # Counter reset (negative delta)

        with patch("psutil.net_if_stats", return_value=fake_stats), patch(
            "psutil.net_io_counters", side_effect=[c1, c2, c3_reset]
        ):
            net1 = svc_net._collect_network_metrics()
            self.assertFalse(net1.is_rate_primed)
            self.assertIsNone(net1.rx_rate_kbps)
            self.assertIsNone(net1.tx_rate_kbps)

            net2 = svc_net._collect_network_metrics()  # 2 seconds elapsed
            self.assertTrue(net2.is_rate_primed)
            self.assertAlmostEqual(net2.rx_rate_kbps, 25.0, places=1)
            self.assertAlmostEqual(net2.tx_rate_kbps, 12.5, places=1)

            net3 = svc_net._collect_network_metrics()  # Negative delta -> filtered out!
            self.assertFalse(net3.is_rate_primed)
            self.assertIsNone(net3.rx_rate_kbps)
            self.assertIsNone(net3.tx_rate_kbps)

    def test_5_health_evaluation_rolling_window_and_missing_sensors(self):
        """A single CPU spike must not trigger CRITICAL; missing sensors are not penalized; reasons explain status."""
        svc = SystemMonitorService()
        svc._recent_cpu_samples.clear()
        svc._recent_cpu_samples.extend([12.0, 15.0, 96.0])  # Single momentary spike to 96%, rolling avg = 41.0%

        cpu_spike = CpuMetrics(
            usage_percent=96.0,
            is_primed=True,
            core_count=16,
            freq_mhz=3200.0,
            temp_c=None,  # Missing CPU temp sensor must NOT be penalized
            temp_sensor_source=None,
            load_avg=(1.2, 1.0, 0.8),
        )
        gpu_missing = GpuMetrics(available=False, name="N/A")
        mem_ok = MemoryMetrics(
            used_gb=6.5,
            total_gb=16.0,
            available_gb=9.5,
            percent=40.6,
            swap_used_gb=0.0,
            swap_total_gb=4.0,
            swap_percent=0.0,
        )
        disk_ok = DiskMetrics(mount_point="/", used_gb=200.0, total_gb=500.0, free_gb=300.0, percent=40.0)

        health = svc._evaluate_health(cpu_spike, gpu_missing, mem_ok, disk_ok)
        self.assertEqual(health.status, "OPTIMAL")
        self.assertEqual(health.component_states.get("cpu_temp"), "N/A")
        self.assertEqual(health.component_states.get("gpu_temp"), "N/A")

        # Sustained critical CPU load + critical RAM + high GPU temp -> CRITICAL with detailed reasons
        svc._recent_cpu_samples.clear()
        svc._recent_cpu_samples.extend([95.0, 96.0, 97.0])
        gpu_hot = GpuMetrics(
            available=True,
            name="RTX 4060",
            usage_percent=98.0,
            temp_c=89.0,
            vram_used_mb=7900.0,
            vram_total_mb=8188.0,
            power_draw_w=75.0,
            power_limit_w=80.0,
            fan_speed_percent=None,
        )
        mem_crit = MemoryMetrics(
            used_gb=15.2,
            total_gb=16.0,
            available_gb=0.8,
            percent=95.0,
            swap_used_gb=2.0,
            swap_total_gb=4.0,
            swap_percent=50.0,
        )
        health_crit = svc._evaluate_health(cpu_spike, gpu_hot, mem_crit, disk_ok)
        self.assertEqual(health_crit.status, "CRITICAL")
        self.assertTrue(any("CPU" in r for r in health_crit.reasons))
        self.assertTrue(any("GPU" in r for r in health_crit.reasons))
        self.assertTrue(any("RAM" in r for r in health_crit.reasons))

    def test_6_history_graphs_and_dashboard_thread_lifecycle(self):
        """60-second history deque, SparklineGraphWidget rendering, and SystemDashboardWidget thread start/stop/restart/shutdown."""
        fake_gpu_line = (
            "NVIDIA GeForce RTX 4060 Laptop GPU, 14, 42, 640, 8188, 15.0, [N/A], 45.0, 80.0, [N/A]\n"
        )
        svc = SystemMonitorService(
            history_maxlen=30,
            command_runner=lambda _args, _t: fake_gpu_line,
        )
        snap1 = svc.collect_snapshot(prime_cpu_if_needed=False)
        snap2 = svc.collect_snapshot(prime_cpu_if_needed=False)
        self.assertGreaterEqual(len(snap2.history.cpu_percent), 1)
        self.assertLessEqual(len(snap2.history.cpu_percent), 30)

        # Render SparklineGraphWidget onto an offscreen QImage
        graph = SparklineGraphWidget(line_color="#38bdf8", secondary_color="#c084fc", fixed_max=100.0)
        graph.resize(180, 44)
        graph.set_series([15.0, 25.0, 40.0, 30.0, 55.0], [5.0, 12.0, 18.0, 14.0, 22.0])
        self.assertEqual(graph.series_lengths(), (5, 5))
        img = QImage(graph.size(), QImage.Format_ARGB32_Premultiplied)
        graph.render(img)
        self.assertFalse(img.isNull())

        # Test ChatWindow + SystemDashboardWidget lifecycle with isolated command_runner
        chat = ChatWindow(start_worker=False)
        chat.system_dashboard.service._command_runner = lambda _args, _t: fake_gpu_line
        try:
            self.assertTrue(chat.system_dashboard.isHidden())
            self.assertFalse(chat.system_dashboard.is_monitoring_active())

            # Toggle open -> thread starts & collects snapshot
            chat.toggle_system_dashboard()
            self.assertFalse(chat.system_dashboard.isHidden())
            _pump_events(250)
            self.assertTrue(chat.system_dashboard.monitor_thread.isRunning())
            self.assertTrue(chat.system_dashboard.is_monitoring_active())
            self.assertIsNotNone(chat.system_dashboard.last_snapshot())

            # Toggle closed -> monitoring pauses
            chat.toggle_system_dashboard()
            self.assertTrue(chat.system_dashboard.isHidden())
            _pump_events(80)
            self.assertFalse(chat.system_dashboard.is_monitoring_active())

            # Toggle open again -> monitoring resumes cleanly
            chat.toggle_system_dashboard()
            self.assertFalse(chat.system_dashboard.isHidden())
            _pump_events(120)
            self.assertTrue(chat.system_dashboard.is_monitoring_active())

            # Shutdown -> monitor_thread and worker_thread both terminate cleanly
            chat.request_shutdown()
            _pump_events(150)
            self.assertFalse(chat.system_dashboard.monitor_thread.isRunning())
        finally:
            chat.system_dashboard.shutdown()
            if chat.worker_thread.isRunning():
                chat.worker_thread.quit()
                chat.worker_thread.wait(1000)
            chat.close()

    def test_7_worker_system_report_command_and_natural_language(self):
        """AntigravityWorker intercepts '/sistem' and natural language system queries
        without calling LLM or spawning real hardware subprocesses."""
        from services.antigravity_worker import AntigravityWorker

        self.assertTrue(SystemMonitorService.is_system_query("/sistem"))
        self.assertTrue(SystemMonitorService.is_system_query("sistem durumu nedir?"))
        self.assertTrue(SystemMonitorService.is_system_query("gpu sıcaklığı kaç derece"))
        self.assertFalse(SystemMonitorService.is_system_query("Python ile sistem durumu özelliğini kodla"))

        with tempfile.TemporaryDirectory() as tmpdir:
            sysfs = Path(tmpdir)
            hwmon = sysfs / "class" / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text("coretemp\n", encoding="utf-8")
            (hwmon / "temp1_input").write_text("48000\n", encoding="utf-8")
            (hwmon / "temp1_label").write_text("Package id 0\n", encoding="utf-8")

            bat = sysfs / "class" / "power_supply" / "BAT0"
            bat.mkdir(parents=True)
            (bat / "type").write_text("Battery\n", encoding="utf-8")
            (bat / "capacity").write_text("76\n", encoding="utf-8")
            (bat / "status").write_text("Discharging\n", encoding="utf-8")

            fake_gpu_out = (
                "NVIDIA GeForce RTX 4060 Laptop GPU, 22, 44, 920, 8188, "
                "18.50, [N/A], 45.00, 80.00, [N/A]\n"
            )
            isolated_sys_service = SystemMonitorService(
                sysfs_root=sysfs,
                command_runner=lambda _args, _t: fake_gpu_out,
            )

            with patch.object(
                isolated_sys_service,
                "_collect_top_processes",
                return_value=[ProcessItem(pid=1234, name="python3", cpu_percent=4.2, memory_mb=128.5)],
            ), patch("psutil.cpu_percent", return_value=18.5):
                worker = AntigravityWorker(
                    client_factory=lambda: (_ for _ in ()).throw(
                        AssertionError("LLM client must not be started for /sistem")
                    ),
                    system_service=isolated_sys_service,
                )
                tokens: list[str] = []
                finished: list[bool] = []
                worker.token_received.connect(tokens.append)
                worker.response_finished.connect(lambda: finished.append(True))

                worker.send_message("/sistem")
                self.assertEqual(len(finished), 1)
                full_md = "".join(tokens)
                self.assertIn("JARVIS Sistem İstihbarat Raporu", full_md)
                self.assertIn("Genel Sağlık Durumu:", full_md)
                self.assertIn("İşlemci (CPU):", full_md)
                self.assertIn("48.0°C", full_md)
                self.assertIn("RTX 4060 Laptop GPU", full_md)
                self.assertIn("BAT0", full_md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
