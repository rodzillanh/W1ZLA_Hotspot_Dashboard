"""Host CPU and memory stats — reads /proc directly, no external tools needed.

CPU is computed from two /proc/stat samples 200ms apart (same approach as the
hotspot SSH command). Memory comes from /proc/meminfo.

Both are sampled in a background thread every 5 seconds and cached so the
Flask request handler returns instantly with no blocking I/O.

Falls back gracefully to N/A on non-Linux hosts (Docker dev environment on
a Mac, Windows, etc.) so this never breaks the app.
"""
import os
import time
import threading
from typing import Optional


def is_docker() -> bool:
    """Standard container-detection heuristic -- Docker always creates this
    marker file inside every container, so its absence means bare metal."""
    return os.path.exists("/.dockerenv")


def is_raspberry_pi() -> bool:
    """True on real Raspberry Pi hardware, via the device-tree model string
    the kernel exposes -- not present on other Linux boards/servers, and
    not present in most containers unless specifically bind-mounted."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return b"Raspberry Pi" in f.read()
    except OSError:
        return False


def is_pi_standalone() -> bool:
    """True only when running directly on Raspberry Pi hardware, not inside
    a container -- gates the Settings 'Host power control' buttons. Rebooting
    or powering off only makes sense for a bare-metal standalone install (see
    install.sh); inside a Docker/Unraid container, "reboot the host" can't
    actually reboot the underlying box from in here."""
    return is_raspberry_pi() and not is_docker()


class HostStats:
    def __init__(self, interval: int = 5):
        self._interval = interval
        self._lock     = threading.Lock()
        self._cpu:  str = "N/A"
        self._temp: str = "N/A"
        self._mem_pct:  str = "N/A"
        self._mem_used: str = ""
        self._mem_total: str = ""
        self._host_uptime_seconds: Optional[float] = None

    def start(self) -> None:
        """Start the background sampling thread (call once at app startup)."""
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu":                 self._cpu,
                "temp":                self._temp,
                "mem_pct":             self._mem_pct,
                "mem_used":            self._mem_used,
                "mem_total":           self._mem_total,
                "host_uptime_seconds": self._host_uptime_seconds,
            }

    # --- internals ---

    def _loop(self) -> None:
        while True:
            try:
                cpu    = self._sample_cpu()
                temp   = self._sample_temp()
                mem    = self._sample_mem()
                uptime = self._sample_uptime()
                with self._lock:
                    self._cpu       = cpu
                    self._temp      = temp
                    self._mem_pct   = mem["pct"]
                    self._mem_used  = mem["used"]
                    self._mem_total = mem["total"]
                    self._host_uptime_seconds = uptime
            except Exception:
                pass
            time.sleep(self._interval)

    @staticmethod
    def _read_proc_stat() -> Optional[list[int]]:
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("cpu "):
                        return [int(x) for x in line.split()[1:]]
        except OSError:
            pass
        return None

    def _sample_cpu(self) -> str:
        s1 = self._read_proc_stat()
        if s1 is None:
            return "N/A"
        time.sleep(0.2)
        s2 = self._read_proc_stat()
        if s2 is None:
            return "N/A"
        # idle is index 3 (user, nice, system, idle, ...)
        idle1  = s1[3]; total1 = sum(s1)
        idle2  = s2[3]; total2 = sum(s2)
        dt = total2 - total1
        if dt == 0:
            return "N/A"
        cpu_pct = (1 - (idle2 - idle1) / dt) * 100
        return f"{cpu_pct:.1f}%"

    @staticmethod
    def _sample_temp() -> str:
        """Same sysfs path and millidegree-C format already used for the
        per-hotspot temperature reading (config.py's SSH_STATUS_CMD /
        monitor.py's _parse_temp) -- read locally here instead of over SSH,
        for whatever host is actually running this app. Absent on non-Pi
        Linux hosts without a thermal zone, and inside most containers
        (Docker doesn't expose the host's own thermal zone by default) --
        degrades to N/A the same way every other stat here does."""
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                raw = f.read().strip()
            return f"{int(raw) / 1000.0:.1f}°C"
        except (OSError, ValueError):
            return "N/A"

    @staticmethod
    def _sample_uptime() -> Optional[float]:
        """Seconds since the HOST kernel itself booted, from /proc/uptime's
        first field -- distinct from the dashboard app's own process uptime
        (app.py's START_TIME), which resets on every container/service
        restart even when the underlying machine hasn't rebooted at all.
        Returns a raw number (None on failure), not a pre-formatted string
        like the other samplers here -- the Quick Settings drawer formats
        it client-side the same way it already formats dashboard_uptime_
        seconds, so both use one shared JS formatter rather than two
        different "Xd Yh" implementations (one server-side, one client-side)."""
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _sample_mem() -> dict:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, val = line.partition(":")
                    info[key.strip()] = int(val.split()[0])  # kB

            total     = info.get("MemTotal", 0)
            available = info.get("MemAvailable", 0)
            used      = total - available
            pct       = (used / total * 100) if total else 0

            def fmt(kb: int) -> str:
                mb = kb / 1024
                return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{round(mb)} MB"

            return {
                "pct":   f"{pct:.0f}%",
                "used":  fmt(used),
                "total": fmt(total),
            }
        except OSError:
            return {"pct": "N/A", "used": "", "total": ""}
