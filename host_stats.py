"""Host CPU and memory stats — reads /proc directly, no external tools needed.

CPU is computed from two /proc/stat samples 200ms apart (same approach as the
hotspot SSH command). Memory comes from /proc/meminfo.

Both are sampled in a background thread every 5 seconds and cached so the
Flask request handler returns instantly with no blocking I/O.

Falls back gracefully to N/A on non-Linux hosts (Docker dev environment on
a Mac, Windows, etc.) so this never breaks the app.
"""
import time
import threading
from typing import Optional


class HostStats:
    def __init__(self, interval: int = 5):
        self._interval = interval
        self._lock     = threading.Lock()
        self._cpu:  str = "N/A"
        self._mem_pct:  str = "N/A"
        self._mem_used: str = ""
        self._mem_total: str = ""

    def start(self) -> None:
        """Start the background sampling thread (call once at app startup)."""
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu":       self._cpu,
                "mem_pct":   self._mem_pct,
                "mem_used":  self._mem_used,
                "mem_total": self._mem_total,
            }

    # --- internals ---

    def _loop(self) -> None:
        while True:
            try:
                cpu = self._sample_cpu()
                mem = self._sample_mem()
                with self._lock:
                    self._cpu       = cpu
                    self._mem_pct   = mem["pct"]
                    self._mem_used  = mem["used"]
                    self._mem_total = mem["total"]
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
