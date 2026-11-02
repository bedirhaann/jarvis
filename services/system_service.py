"""JARVIS Stage 7 — System Intelligence Service.

Read-only, zero-root system hardware & telemetry collector supporting:
- Verified CPU temperature discovery (coretemp/k10temp/zenpower, x86_pkg_temp/TCPU)
- Dynamic battery & AC adapter discovery (/sys/class/power_supply/BAT*, ACAD/AC*)
- NVIDIA RTX GPU metrics via strict whitelisted nvidia-smi (utilization, temp, VRAM, power draw/limit, fan)
- Accurate delta-based CPU and Network RX/TX sampling (first sample priming, negative delta filtering)
- Multi-sample rolling health evaluation with transparent human-readable explanations
- Bounded 60-second in-memory history buffers (collections.deque) for real-time sparkline graphs
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


# Strictly whitelisted read-only binaries (never combined with user input, shell=False)
ALLOWED_BINARIES = {
    "nvidia-smi": ("/usr/bin/nvidia-smi", "/bin/nvidia-smi"),
    "sensors": ("/usr/bin/sensors", "/bin/sensors"),
}

# Verified CPU hwmon driver names (excludes generic ACPI/board/wifi/nvme sensors)
VERIFIED_CPU_HWMON_NAMES = {
    "coretemp",
    "k10temp",
    "zenpower",
    "cpu_thermal",
    "fam15h_power",
    "atk0110",
}

# Verified CPU thermal_zone type names (excludes acpitz, INT3400, pch_*, iwlwifi, etc.)
VERIFIED_CPU_THERMAL_ZONE_TYPES = {
    "x86_pkg_temp",
    "tcpu",
    "cpu-thermal",
    "cpu_thermal",
    "pkg_temp",
    "soc_thermal",
    "cpu",
}

_TR_ASCII_MAP = str.maketrans({
    "ı": "i",
    "İ": "i",
    "I": "i",
    "ğ": "g",
    "Ğ": "g",
    "ü": "u",
    "Ü": "u",
    "ş": "s",
    "Ş": "s",
    "ö": "o",
    "Ö": "o",
    "ç": "c",
    "Ç": "c",
})

_SYSTEM_COMMAND_RE = re.compile(
    r"^\s*(?:/plan\s+)?(?:/sistem|/system|/donanim|/hardware|/durum)\b(.*)$",
    re.IGNORECASE,
)

_SYSTEM_QUERY_PHRASES = (
    "sistem durumu",
    "sistem sagligi",
    "sistem raporu",
    "donanim durumu",
    "cpu kullanimi",
    "cpu sicakligi",
    "islemci sicakligi",
    "islemci kullanimi",
    "gpu kullanimi",
    "gpu sicakligi",
    "ekran karti sicakligi",
    "ekran karti kullanimi",
    "vram kullanimi",
    "ram kullanimi",
    "bellek kullanimi",
    "disk kullanimi",
    "depolama durumu",
    "batarya durumu",
    "pil durumu",
    "pil yuzdesi",
    "ag trafigi",
    "aktif surecler",
    "bilgisayarin durumu",
)

_META_DEVELOPMENT_PHRASES = (
    "ozelligini",
    "ozeliigini",
    "servis kullanarak",
    "ekle",
    "kodla",
    "planla",
    "entegrasyon",
    "implement",
    "stage 7",
    "mimarisini",
    "tasarla",
)


def _normalize_tr(text: str) -> str:
    return (text or "").translate(_TR_ASCII_MAP).lower().strip()


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or "n/a" in cleaned.lower() or "not supported" in cleaned.lower():
        return None
    cleaned = cleaned.replace("%", "").replace("W", "").replace("MiB", "").replace("C", "").strip()
    try:
        val = float(cleaned)
        if val != val:  # NaN check
            return None
        return val
    except ValueError:
        return None


def fmt_metric(value: float | int | None, suffix: str = "", decimals: int = 1) -> str:
    """Format a numeric metric or return 'N/A' when value is None."""
    if value is None:
        return "N/A"
    if decimals <= 0:
        return f"{int(round(value))}{suffix}"
    return f"{value:.{decimals}f}{suffix}"


@dataclass
class CpuMetrics:
    usage_percent: float | None  # None on first unprimed sample
    is_primed: bool
    core_count: int
    freq_mhz: float | None
    temp_c: float | None
    temp_sensor_source: str | None
    load_avg: tuple[float, float, float]


@dataclass
class GpuMetrics:
    available: bool
    name: str
    usage_percent: float | None = None
    temp_c: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None
    fan_speed_percent: float | None = None


@dataclass
class MemoryMetrics:
    used_gb: float
    total_gb: float
    available_gb: float
    percent: float
    swap_used_gb: float
    swap_total_gb: float
    swap_percent: float


@dataclass
class DiskMetrics:
    mount_point: str
    used_gb: float
    total_gb: float
    free_gb: float
    percent: float


@dataclass
class NetworkMetrics:
    is_online: bool
    active_interfaces: list[str]
    rx_rate_kbps: float | None  # None on first sample before delta is available
    tx_rate_kbps: float | None
    is_rate_primed: bool
    total_rx_mb: float
    total_tx_mb: float


@dataclass
class BatteryMetrics:
    present: bool
    device_name: str | None = None
    percent: float | None = None
    is_plugged: bool | None = None
    adapter_name: str | None = None
    status: str = "N/A"


@dataclass
class ProcessItem:
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float


@dataclass
class SystemHealthReport:
    status: str  # "OPTIMAL" | "MODERATE" | "HIGH LOAD" | "CRITICAL"
    summary: str
    reasons: list[str] = field(default_factory=list)
    component_states: dict[str, str] = field(default_factory=dict)


@dataclass
class SystemHistorySnapshot:
    """Immutable copy of the 60-second rolling history deques for UI graphing."""

    cpu_percent: list[float]
    gpu_percent: list[float]
    ram_percent: list[float]
    net_rx_kbps: list[float]
    net_tx_kbps: list[float]


@dataclass
class SystemSnapshot:
    timestamp: str
    cpu: CpuMetrics
    gpu: GpuMetrics
    memory: MemoryMetrics
    disk: DiskMetrics
    network: NetworkMetrics
    battery: BatteryMetrics
    top_processes: list[ProcessItem]
    health: SystemHealthReport
    history: SystemHistorySnapshot


class SystemMonitorService:
    """Thread-safe, read-only system intelligence collector with delta sampling
    and 60-second rolling history."""

    def __init__(
        self,
        history_maxlen: int = 30,
        sysfs_root: str | Path = "/sys",
        procfs_root: str | Path = "/proc",
        command_runner: Callable[[list[str], float], str | None] | None = None,
        command_timeout: float = 2.0,
        monotonic_clock: Callable[[], float] | None = None,
    ):
        self.history_maxlen = max(10, history_maxlen)
        self.sysfs_root = Path(sysfs_root)
        self.procfs_root = Path(procfs_root)
        self.command_timeout = command_timeout
        self._command_runner = command_runner or self._default_command_runner
        self._monotonic_clock = monotonic_clock or time.monotonic

        # Delta sampling state for CPU and Network
        self._cpu_primed = False
        self._prev_proc_cpu_times: tuple[int, int] | None = None
        self._prev_net_counters: tuple[float, int, int] | None = None  # (monotonic_ts, rx_bytes, tx_bytes)

        # Rolling CPU window for anti-spike health evaluation (last 3 valid samples)
        self._recent_cpu_samples: deque[float] = deque(maxlen=3)

        # 60-second in-memory history deques (30 samples @ 2s interval = 60s)
        self._cpu_history: deque[float] = deque(maxlen=self.history_maxlen)
        self._gpu_history: deque[float] = deque(maxlen=self.history_maxlen)
        self._ram_history: deque[float] = deque(maxlen=self.history_maxlen)
        self._net_rx_history: deque[float] = deque(maxlen=self.history_maxlen)
        self._net_tx_history: deque[float] = deque(maxlen=self.history_maxlen)

    # ------------------------------------------------------------------
    # Safe Whitelisted Subprocess Execution
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_whitelisted_binary(binary_key: str) -> str | None:
        candidates = ALLOWED_BINARIES.get(binary_key, ())
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        found = shutil.which(binary_key)
        return found

    def _default_command_runner(self, args: list[str], timeout: float) -> str | None:
        if not args:
            return None
        binary_key = os.path.basename(args[0])
        if binary_key not in ALLOWED_BINARIES:
            return None
        resolved = self._resolve_whitelisted_binary(binary_key)
        if not resolved:
            return None

        safe_cmd = [resolved] + [str(a) for a in args[1:]]
        try:
            completed = subprocess.run(
                safe_cmd,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                return None
            return completed.stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 1. Verified CPU Temperature & Metrics
    # ------------------------------------------------------------------
    def _read_verified_cpu_temperature(self) -> tuple[float | None, str | None]:
        """Read CPU temperature strictly from verified CPU sensors.
        Never reports generic ACPI/motherboard/wifi sensors as CPU temperature."""
        # Priority 1: /sys/class/hwmon/hwmon*/ with verified CPU driver name
        hwmon_base = self.sysfs_root / "class" / "hwmon"
        if hwmon_base.is_dir():
            try:
                for hw_dir in sorted(hwmon_base.iterdir()):
                    name_file = hw_dir / "name"
                    if not name_file.is_file():
                        continue
                    driver_name = name_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
                    if driver_name not in VERIFIED_CPU_HWMON_NAMES:
                        continue

                    best_temp: float | None = None
                    best_label: str = driver_name
                    for input_path in sorted(hw_dir.glob("temp*_input")):
                        try:
                            raw_milli = float(input_path.read_text(encoding="utf-8").strip())
                            temp_c = raw_milli / 1000.0 if raw_milli > 200 else raw_milli
                        except (OSError, ValueError):
                            continue
                        if not (5.0 <= temp_c <= 125.0):
                            continue

                        label_path = hw_dir / input_path.name.replace("_input", "_label")
                        label = ""
                        if label_path.is_file():
                            try:
                                label = label_path.read_text(encoding="utf-8", errors="ignore").strip().lower()
                            except OSError:
                                label = ""

                        # Prefer Package / Tctl / Tdie reading right away
                        if any(k in label for k in ("package", "tctl", "tdie", "cpu")):
                            return round(temp_c, 1), f"{driver_name}:{label}"
                        if best_temp is None or temp_c > best_temp:
                            best_temp = temp_c
                            best_label = f"{driver_name}:{label or input_path.stem}"

                    if best_temp is not None:
                        return round(best_temp, 1), best_label
            except OSError:
                pass

        # Priority 2: /sys/class/thermal/thermal_zone*/ with verified CPU type
        thermal_base = self.sysfs_root / "class" / "thermal"
        if thermal_base.is_dir():
            try:
                # Prefer x86_pkg_temp first if available
                zones = sorted(thermal_base.glob("thermal_zone*"))
                matched_zones: list[tuple[int, float, str]] = []
                for z_dir in zones:
                    type_file = z_dir / "type"
                    temp_file = z_dir / "temp"
                    if not (type_file.is_file() and temp_file.is_file()):
                        continue
                    try:
                        z_type = type_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
                        if z_type not in VERIFIED_CPU_THERMAL_ZONE_TYPES:
                            continue
                        raw_val = float(temp_file.read_text(encoding="utf-8").strip())
                        temp_c = raw_val / 1000.0 if raw_val > 200 else raw_val
                        if 5.0 <= temp_c <= 125.0:
                            priority = 0 if z_type == "x86_pkg_temp" else 1
                            matched_zones.append((priority, round(temp_c, 1), f"thermal:{z_type}"))
                    except (OSError, ValueError):
                        continue
                if matched_zones:
                    matched_zones.sort(key=lambda item: item[0])
                    _, temp_val, src = matched_zones[0]
                    return temp_val, src
            except OSError:
                pass

        return None, None

    def _read_proc_cpu_times(self) -> tuple[int, int] | None:
        stat_file = self.procfs_root / "stat"
        if not stat_file.is_file():
            return None
        try:
            first_line = stat_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            parts = first_line.split()
            if len(parts) < 5 or parts[0] != "cpu":
                return None
            values = [int(x) for x in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            return idle, total
        except (OSError, ValueError, IndexError):
            return None

    def _collect_cpu_metrics(self, prime_if_needed: bool = False) -> CpuMetrics:
        usage_percent: float | None = None
        is_primed = self._cpu_primed

        if psutil is not None:
            if not self._cpu_primed:
                # First call to psutil.cpu_percent(interval=None) is meaningless (0.0);
                # discard it and optionally do a brief 0.08s prime if requested.
                psutil.cpu_percent(interval=None)
                self._cpu_primed = True
                if prime_if_needed:
                    usage_percent = float(psutil.cpu_percent(interval=0.08))
                    is_primed = True
                else:
                    usage_percent = None
                    is_primed = False
            else:
                raw_usage = float(psutil.cpu_percent(interval=None))
                usage_percent = max(0.0, min(100.0, raw_usage))
                is_primed = True
        else:
            times = self._read_proc_cpu_times()
            if times is not None:
                if self._prev_proc_cpu_times is None:
                    self._prev_proc_cpu_times = times
                    self._cpu_primed = True
                    is_primed = False
                else:
                    prev_idle, prev_total = self._prev_proc_cpu_times
                    cur_idle, cur_total = times
                    self._prev_proc_cpu_times = times
                    delta_total = cur_total - prev_total
                    delta_idle = cur_idle - prev_idle
                    if delta_total > 0 and delta_idle >= 0:
                        usage_percent = max(
                            0.0,
                            min(100.0, (1.0 - (delta_idle / float(delta_total))) * 100.0),
                        )
                        is_primed = True

        if usage_percent is not None:
            self._recent_cpu_samples.append(usage_percent)
            self._cpu_history.append(usage_percent)

        core_count = os.cpu_count() or 1
        freq_mhz: float | None = None
        if psutil is not None:
            try:
                freq = psutil.cpu_freq()
                if freq is not None and freq.current and freq.current > 0:
                    freq_mhz = round(float(freq.current), 0)
            except Exception:
                pass

        try:
            load_avg = os.getloadavg()
        except OSError:
            load_avg = (0.0, 0.0, 0.0)

        temp_c, sensor_src = self._read_verified_cpu_temperature()

        return CpuMetrics(
            usage_percent=usage_percent,
            is_primed=is_primed,
            core_count=core_count,
            freq_mhz=freq_mhz,
            temp_c=temp_c,
            temp_sensor_source=sensor_src,
            load_avg=(round(load_avg[0], 2), round(load_avg[1], 2), round(load_avg[2], 2)),
        )

    # ------------------------------------------------------------------
    # 2. Dynamic Battery & AC Adapter Detection
    # ------------------------------------------------------------------
    def _collect_battery_metrics(self) -> BatteryMetrics:
        power_base = self.sysfs_root / "class" / "power_supply"
        bat_dirs: list[Path] = []
        ac_dirs: list[Path] = []

        if power_base.is_dir():
            try:
                for entry in sorted(power_base.iterdir()):
                    name_upper = entry.name.upper()
                    type_file = entry / "type"
                    dev_type = ""
                    if type_file.is_file():
                        try:
                            dev_type = type_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
                        except OSError:
                            dev_type = ""

                    if name_upper.startswith("BAT") or dev_type == "battery":
                        bat_dirs.append(entry)
                    elif (
                        name_upper.startswith(("AC", "ACAD", "ADP", "MAINS"))
                        or dev_type == "mains"
                    ):
                        ac_dirs.append(entry)
            except OSError:
                pass

        if not bat_dirs:
            return BatteryMetrics(present=False, status="Batarya Yok")

        bat_dir = bat_dirs[0]
        percent: float | None = None
        cap_file = bat_dir / "capacity"
        if cap_file.is_file():
            try:
                val = float(cap_file.read_text(encoding="utf-8").strip())
                if 0.0 <= val <= 100.0:
                    percent = round(val, 1)
            except (OSError, ValueError):
                pass

        status_raw = "Unknown"
        status_file = bat_dir / "status"
        if status_file.is_file():
            try:
                status_raw = status_file.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                pass

        is_plugged: bool | None = None
        adapter_name: str | None = None
        for ac_dir in ac_dirs:
            online_file = ac_dir / "online"
            if online_file.is_file():
                try:
                    online_val = online_file.read_text(encoding="utf-8").strip()
                    if online_val in ("0", "1"):
                        is_plugged = online_val == "1"
                        adapter_name = ac_dir.name
                        if is_plugged:
                            break
                except OSError:
                    continue

        if is_plugged is None and status_raw.lower() in ("charging", "full", "not charging"):
            is_plugged = True
        elif is_plugged is None and status_raw.lower() == "discharging":
            is_plugged = False

        status_tr_map = {
            "charging": "Şarj Oluyor",
            "discharging": "Pil Kullanılıyor",
            "full": "Tam Dolu",
            "not charging": "Prize Takılı (Koruma Modu)",
            "unknown": "Prize Takılı" if is_plugged else "Bilinmiyor",
        }
        status_tr = status_tr_map.get(status_raw.lower(), status_raw)

        return BatteryMetrics(
            present=True,
            device_name=bat_dir.name,
            percent=percent,
            is_plugged=is_plugged,
            adapter_name=adapter_name,
            status=status_tr,
        )

    # ------------------------------------------------------------------
    # 3. NVIDIA RTX GPU Metrics (Whitelist + N/A Support)
    # ------------------------------------------------------------------
    def _collect_gpu_metrics(self) -> GpuMetrics:
        query_fields = (
            "name,utilization.gpu,temperature.gpu,memory.used,memory.total,"
            "power.draw,power.limit,enforced.power.limit,power.default_limit,fan.speed"
        )
        out = self._command_runner(
            [
                "nvidia-smi",
                f"--query-gpu={query_fields}",
                "--format=csv,noheader,nounits",
            ],
            self.command_timeout,
        )
        if not out or not out.strip():
            return GpuMetrics(available=False, name="N/A")

        first_line = out.strip().splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        if len(parts) < 5:
            return GpuMetrics(available=False, name="N/A")

        name = parts[0] or "NVIDIA GPU"
        usage = _parse_optional_float(parts[1])
        temp_c = _parse_optional_float(parts[2])
        vram_used = _parse_optional_float(parts[3])
        vram_total = _parse_optional_float(parts[4])
        power_draw = _parse_optional_float(parts[5]) if len(parts) > 5 else None

        # Check power.limit -> enforced.power.limit -> power.default_limit
        power_limit = _parse_optional_float(parts[6]) if len(parts) > 6 else None
        if power_limit is None and len(parts) > 7:
            power_limit = _parse_optional_float(parts[7])
        if power_limit is None and len(parts) > 8:
            power_limit = _parse_optional_float(parts[8])

        fan_speed = _parse_optional_float(parts[9]) if len(parts) > 9 else None

        if usage is not None:
            usage = max(0.0, min(100.0, usage))
            self._gpu_history.append(usage)

        return GpuMetrics(
            available=True,
            name=name,
            usage_percent=usage,
            temp_c=temp_c,
            vram_used_mb=vram_used,
            vram_total_mb=vram_total,
            power_draw_w=power_draw,
            power_limit_w=power_limit,
            fan_speed_percent=fan_speed,
        )

    # ------------------------------------------------------------------
    # 4. Memory, Disk, Network (Delta Sampling) & Top Processes
    # ------------------------------------------------------------------
    def _collect_memory_metrics(self) -> MemoryMetrics:
        if psutil is not None:
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            used_gb = (vm.total - vm.available) / (1024.0 ** 3)
            total_gb = vm.total / (1024.0 ** 3)
            avail_gb = vm.available / (1024.0 ** 3)
            pct = float(vm.percent)
            sw_used = sw.used / (1024.0 ** 3)
            sw_total = sw.total / (1024.0 ** 3)
            sw_pct = float(sw.percent)
        else:
            used_gb = total_gb = avail_gb = pct = sw_used = sw_total = sw_pct = 0.0

        self._ram_history.append(max(0.0, min(100.0, pct)))

        return MemoryMetrics(
            used_gb=round(used_gb, 2),
            total_gb=round(total_gb, 2),
            available_gb=round(avail_gb, 2),
            percent=round(pct, 1),
            swap_used_gb=round(sw_used, 2),
            swap_total_gb=round(sw_total, 2),
            swap_percent=round(sw_pct, 1),
        )

    def _collect_disk_metrics(self, mount_point: str = "/") -> DiskMetrics:
        try:
            usage = shutil.disk_usage(mount_point)
            total_gb = usage.total / (1024.0 ** 3)
            used_gb = usage.used / (1024.0 ** 3)
            free_gb = usage.free / (1024.0 ** 3)
            pct = (usage.used / float(usage.total)) * 100.0 if usage.total > 0 else 0.0
        except OSError:
            total_gb = used_gb = free_gb = pct = 0.0

        return DiskMetrics(
            mount_point=mount_point,
            used_gb=round(used_gb, 1),
            total_gb=round(total_gb, 1),
            free_gb=round(free_gb, 1),
            percent=round(pct, 1),
        )

    def _collect_network_metrics(self, now_monotonic: float | None = None) -> NetworkMetrics:
        now = float(now_monotonic) if now_monotonic is not None else float(self._monotonic_clock())
        active_ifaces: list[str] = []
        rx_bytes = 0
        tx_bytes = 0

        if psutil is not None:
            try:
                stats = psutil.net_if_stats()
                per_nic = psutil.net_io_counters(pernic=True)
                for nic, st in stats.items():
                    if nic == "lo" or not st.isup:
                        continue
                    active_ifaces.append(nic)
                    io = per_nic.get(nic)
                    if io is not None:
                        rx_bytes += int(io.bytes_recv)
                        tx_bytes += int(io.bytes_sent)
            except Exception:
                pass

        rx_rate_kbps: float | None = None
        tx_rate_kbps: float | None = None
        is_rate_primed = False

        if self._prev_net_counters is not None:
            prev_ts, prev_rx, prev_tx = self._prev_net_counters
            dt = now - prev_ts
            delta_rx = rx_bytes - prev_rx
            delta_tx = tx_bytes - prev_tx
            # Filter out invalid time intervals or negative counter resets
            if dt >= 0.05 and delta_rx >= 0 and delta_tx >= 0:
                rx_rate_kbps = round((delta_rx / 1024.0) / dt, 1)
                tx_rate_kbps = round((delta_tx / 1024.0) / dt, 1)
                is_rate_primed = True
                self._net_rx_history.append(rx_rate_kbps)
                self._net_tx_history.append(tx_rate_kbps)

        self._prev_net_counters = (now, rx_bytes, tx_bytes)

        return NetworkMetrics(
            is_online=len(active_ifaces) > 0,
            active_interfaces=active_ifaces,
            rx_rate_kbps=rx_rate_kbps,
            tx_rate_kbps=tx_rate_kbps,
            is_rate_primed=is_rate_primed,
            total_rx_mb=round(rx_bytes / (1024.0 ** 2), 1),
            total_tx_mb=round(tx_bytes / (1024.0 ** 2), 1),
        )

    def _collect_top_processes(self, limit: int = 5) -> list[ProcessItem]:
        if psutil is None:
            return []
        items: list[ProcessItem] = []
        try:
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    info = proc.info
                    mem_info = info.get("memory_info")
                    rss_mb = (mem_info.rss / (1024.0 ** 2)) if mem_info else 0.0
                    cpu_p = float(info.get("cpu_percent") or 0.0)
                    items.append(
                        ProcessItem(
                            pid=int(info.get("pid") or 0),
                            name=str(info.get("name") or "unknown")[:28],
                            cpu_percent=round(cpu_p, 1),
                            memory_mb=round(rss_mb, 1),
                        )
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    continue
        except Exception:
            return []

        items.sort(key=lambda p: (p.memory_mb, p.cpu_percent), reverse=True)
        return items[:limit]

    # ------------------------------------------------------------------
    # 5. Transparent, Anti-Spike Health Evaluation
    # ------------------------------------------------------------------
    def _evaluate_health(
        self,
        cpu: CpuMetrics,
        gpu: GpuMetrics,
        memory: MemoryMetrics,
        disk: DiskMetrics,
    ) -> SystemHealthReport:
        """Evaluate CPU, GPU, RAM, Disk, and Temperatures independently.
        Uses sustained rolling CPU average to prevent single-sample spike false alarms.
        Missing sensors (None) are never penalized."""
        reasons: list[str] = []
        comp: dict[str, str] = {}
        severity = 0  # 0=OPTIMAL, 1=MODERATE, 2=HIGH LOAD, 3=CRITICAL

        def _escalate(level: int):
            nonlocal severity
            if level > severity:
                severity = level

        # 1. CPU Load (sustained multi-sample evaluation)
        if len(self._recent_cpu_samples) >= 2:
            avg_cpu = sum(self._recent_cpu_samples) / len(self._recent_cpu_samples)
            min_recent_cpu = min(self._recent_cpu_samples)
            if min_recent_cpu >= 92.0 and len(self._recent_cpu_samples) >= 3:
                _escalate(3)
                comp["cpu"] = "CRITICAL"
                reasons.append(f"Sürekli çok yüksek CPU yükü (Ort: %{avg_cpu:.1f})")
            elif avg_cpu >= 80.0:
                _escalate(2)
                comp["cpu"] = "HIGH LOAD"
                reasons.append(f"Yüksek CPU kullanımı (Ort: %{avg_cpu:.1f})")
            elif avg_cpu >= 65.0:
                _escalate(1)
                comp["cpu"] = "MODERATE"
                reasons.append(f"Orta seviye CPU yükü (Ort: %{avg_cpu:.1f})")
            else:
                comp["cpu"] = "OPTIMAL"
        else:
            comp["cpu"] = "OPTIMAL"

        # 2. CPU Temperature (if verified sensor available)
        if cpu.temp_c is not None:
            if cpu.temp_c >= 92.0:
                _escalate(3)
                comp["cpu_temp"] = "CRITICAL"
                reasons.append(f"Kritik CPU sıcaklığı ({cpu.temp_c:.1f}°C)")
            elif cpu.temp_c >= 82.0:
                _escalate(2)
                comp["cpu_temp"] = "HIGH LOAD"
                reasons.append(f"Yüksek CPU sıcaklığı ({cpu.temp_c:.1f}°C)")
            elif cpu.temp_c >= 74.0:
                _escalate(1)
                comp["cpu_temp"] = "MODERATE"
                reasons.append(f"CPU sıcaklığı artıyor ({cpu.temp_c:.1f}°C)")
            else:
                comp["cpu_temp"] = "OPTIMAL"
        else:
            comp["cpu_temp"] = "N/A"

        # 3. GPU Temperature & VRAM (if available)
        if gpu.available and gpu.temp_c is not None:
            if gpu.temp_c >= 88.0:
                _escalate(3)
                comp["gpu_temp"] = "CRITICAL"
                reasons.append(f"Kritik GPU sıcaklığı ({gpu.temp_c:.1f}°C)")
            elif gpu.temp_c >= 80.0:
                _escalate(2)
                comp["gpu_temp"] = "HIGH LOAD"
                reasons.append(f"Yüksek GPU sıcaklığı ({gpu.temp_c:.1f}°C)")
            else:
                comp["gpu_temp"] = "OPTIMAL"
        else:
            comp["gpu_temp"] = "N/A"

        # 4. RAM Usage
        if memory.percent >= 93.0:
            _escalate(3)
            comp["ram"] = "CRITICAL"
            reasons.append(f"Kritik RAM doluluğu (%{memory.percent:.1f})")
        elif memory.percent >= 84.0:
            _escalate(2)
            comp["ram"] = "HIGH LOAD"
            reasons.append(f"Yüksek RAM kullanımı (%{memory.percent:.1f})")
        elif memory.percent >= 72.0:
            _escalate(1)
            comp["ram"] = "MODERATE"
            reasons.append(f"Orta seviye RAM kullanımı (%{memory.percent:.1f})")
        else:
            comp["ram"] = "OPTIMAL"

        # 5. Disk Usage
        if disk.percent >= 95.0:
            _escalate(3)
            comp["disk"] = "CRITICAL"
            reasons.append(f"Disk alanı kritik seviyede dolu (%{disk.percent:.1f})")
        elif disk.percent >= 88.0:
            _escalate(2)
            comp["disk"] = "HIGH LOAD"
            reasons.append(f"Disk doluluk oranı yüksek (%{disk.percent:.1f})")
        else:
            comp["disk"] = "OPTIMAL"

        status_labels = {
            0: "OPTIMAL",
            1: "MODERATE",
            2: "HIGH LOAD",
            3: "CRITICAL",
        }
        status = status_labels.get(severity, "OPTIMAL")

        if not reasons:
            evaluated = ["CPU", "RAM", "Disk"]
            if cpu.temp_c is not None:
                evaluated.append("CPU Sıcaklığı")
            if gpu.available:
                evaluated.append("GPU")
            summary = f"Tüm izlenen bileşenler ({', '.join(evaluated)}) normal çalışma aralığında."
        else:
            summary = " • ".join(reasons)

        return SystemHealthReport(
            status=status,
            summary=summary,
            reasons=reasons,
            component_states=comp,
        )

    # ------------------------------------------------------------------
    # Snapshot & Chat Formatting
    # ------------------------------------------------------------------
    def collect_snapshot(self, prime_cpu_if_needed: bool = False) -> SystemSnapshot:
        cpu = self._collect_cpu_metrics(prime_if_needed=prime_cpu_if_needed)
        gpu = self._collect_gpu_metrics()
        memory = self._collect_memory_metrics()
        disk = self._collect_disk_metrics("/")
        network = self._collect_network_metrics()
        battery = self._collect_battery_metrics()
        processes = self._collect_top_processes(limit=5)
        health = self._evaluate_health(cpu, gpu, memory, disk)
        history = SystemHistorySnapshot(
            cpu_percent=list(self._cpu_history),
            gpu_percent=list(self._gpu_history),
            ram_percent=list(self._ram_history),
            net_rx_kbps=list(self._net_rx_history),
            net_tx_kbps=list(self._net_tx_history),
        )
        return SystemSnapshot(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            cpu=cpu,
            gpu=gpu,
            memory=memory,
            disk=disk,
            network=network,
            battery=battery,
            top_processes=processes,
            health=health,
            history=history,
        )

    @staticmethod
    def is_system_query(message: str) -> bool:
        if not message or not message.strip():
            return False
        if _SYSTEM_COMMAND_RE.match(message):
            return True
        norm = _normalize_tr(message)
        if norm.startswith("/plan "):
            norm = norm[6:].strip()
        if any(meta in norm for meta in _META_DEVELOPMENT_PHRASES):
            return False
        return any(phrase in norm for phrase in _SYSTEM_QUERY_PHRASES)

    def format_system_markdown(self, snapshot: SystemSnapshot) -> str:
        cpu = snapshot.cpu
        gpu = snapshot.gpu
        mem = snapshot.memory
        disk = snapshot.disk
        net = snapshot.network
        bat = snapshot.battery

        cpu_usage_str = (
            fmt_metric(cpu.usage_percent, "%", 1) if cpu.is_primed else "Ölçülüyor..."
        )
        cpu_temp_str = fmt_metric(cpu.temp_c, "°C", 1)
        cpu_freq_str = fmt_metric(cpu.freq_mhz, " MHz", 0)

        lines = [
            f"### 🖥️ JARVIS Sistem İstihbarat Raporu ({snapshot.timestamp})",
            "",
            f"* **Genel Sağlık Durumu:** **{snapshot.health.status}** — {snapshot.health.summary}",
            f"* **İşlemci (CPU):** Kullanım: **{cpu_usage_str}** | Sıcaklık: **{cpu_temp_str}** | Frekans: {cpu_freq_str} ({cpu.core_count} Çekirdek)",
        ]

        if gpu.available:
            vram_str = (
                f"{fmt_metric(gpu.vram_used_mb, '', 0)} / {fmt_metric(gpu.vram_total_mb, ' MiB', 0)}"
                if gpu.vram_used_mb is not None and gpu.vram_total_mb is not None
                else "N/A"
            )
            pwr_str = (
                f"{fmt_metric(gpu.power_draw_w, ' W', 1)} / Limit: {fmt_metric(gpu.power_limit_w, ' W', 0)}"
                if gpu.power_draw_w is not None
                else "N/A"
            )
            fan_str = fmt_metric(gpu.fan_speed_percent, "%", 0)
            lines.append(
                f"* **Ekran Kartı ({gpu.name}):** Kullanım: **{fmt_metric(gpu.usage_percent, '%', 0)}** | "
                f"Sıcaklık: **{fmt_metric(gpu.temp_c, '°C', 0)}** | VRAM: **{vram_str}** | "
                f"Güç: {pwr_str} | Fan: {fan_str}"
            )
        else:
            lines.append("* **Ekran Kartı (GPU):** N/A")

        lines.extend([
            f"* **Bellek (RAM):** **{mem.used_gb:.2f} / {mem.total_gb:.2f} GB** (**%{mem.percent:.1f}**) | Swap: %{mem.swap_percent:.1f}",
            f"* **Depolama ({disk.mount_point}):** **{disk.used_gb:.1f} / {disk.total_gb:.1f} GB** (**%{disk.percent:.1f}**, Boş: {disk.free_gb:.1f} GB)",
        ])

        if net.is_rate_primed:
            rate_str = f"↓ {fmt_metric(net.rx_rate_kbps, ' KB/s', 1)} | ↑ {fmt_metric(net.tx_rate_kbps, ' KB/s', 1)}"
        else:
            rate_str = "Ölçülüyor..."
        iface_str = ", ".join(net.active_interfaces) if net.active_interfaces else "Çevrimdışı"
        lines.append(
            f"* **Ağ ({iface_str}):** {rate_str} (Toplam: ↓ {net.total_rx_mb:.1f} MB / ↑ {net.total_tx_mb:.1f} MB)"
        )

        if bat.present:
            plug_str = "Prize Takılı" if bat.is_plugged else "Pilde"
            lines.append(
                f"* **Batarya ({bat.device_name}):** **{fmt_metric(bat.percent, '%', 0)}** — {bat.status} ({plug_str})"
            )
        else:
            lines.append("* **Batarya:** N/A (Masaüstü / Batarya Yok)")

        if snapshot.top_processes:
            lines.append("")
            lines.append("#### En Çok Kaynak Kullanan Süreçler")
            for p in snapshot.top_processes[:5]:
                lines.append(
                    f"* `{p.name}` (PID {p.pid}) — RAM: **{p.memory_mb:.1f} MB** | CPU: %{p.cpu_percent:.1f}"
                )

        return "\n".join(lines)

    def get_system_report(self) -> tuple[str, SystemSnapshot]:
        snap = self.collect_snapshot(prime_cpu_if_needed=True)
        return self.format_system_markdown(snap), snap
