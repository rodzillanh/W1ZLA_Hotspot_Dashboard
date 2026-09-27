"""Hotspot health history -- uptime, temperature, CPU and reboots over time.

Backs the optional Hotspot Health card, the Health section of each
hotspot's own drawer, and the "running warm" hotspot notifications.

Nothing new is polled: every SSH poll already returns temperature, CPU and
uptime (monitor.py), and this module just samples monitor.snapshot() every
SAMPLE_SEC into its own small SQLite file (health.db, same "own db file,
connection per call" shape as storage_activity.py), kept RETENTION_DAYS.
One row per hotspot per sample: 30 days x 288/day = ~8,600 rows each.

Derived here, not stored:
  * outages  -- runs of offline samples (a portable hotspot that's away is
                recorded as `away` and never counts as downtime)
  * reboots  -- uptime going DOWN between two consecutive online samples,
                which catches a reboot too quick to ever show offline
  * warm     -- runs of samples at/above the warm threshold (°C)

Every public function swallows its own errors (returns empty results), the
same degrade-silently contract as every integration in this app.
"""
import os
import re
import sqlite3
import threading
import time

import config

DB_PATH = os.path.join(config.CONFIG_DIR, "health.db")
SAMPLE_SEC = 300
RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    hotspot_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    online INTEGER NOT NULL,
    away INTEGER NOT NULL DEFAULT 0,
    temp_c REAL,
    cpu REAL,
    uptime_s INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_hs_ts ON samples(hotspot_id, ts);
"""

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    return c


# ---- parsing the display strings monitor.py produces ----

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_UP_PART = re.compile(r"(\d+)\s*(years?|y|weeks?|w|days?|d|hours?|h|minutes?|mins?|m)\b", re.I)
_UP_MULT = {"y": 365 * 86400, "w": 7 * 86400, "d": 86400, "h": 3600, "m": 60}


def parse_temp_c(val):
    """"48.2°C" -> 48.2 (°F strings are converted); None if not a reading."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = _NUM.search(str(val))
    if not m:
        return None
    t = float(m.group(0))
    return round((t - 32) * 5 / 9, 1) if "F" in str(val).upper() else t


def parse_pct(val):
    if val is None:
        return None
    m = _NUM.search(str(val))
    return float(m.group(0)) if m else None


def parse_uptime(val):
    """`uptime -p` text ("2 weeks, 3 days, 4 hours, 5 minutes") or the
    compact "3d 4h 12m" form -> seconds; None if nothing recognizable."""
    if not val:
        return None
    total, hit = 0, False
    for n, unit in _UP_PART.findall(str(val)):
        key = unit[0].lower()
        if unit.lower().startswith("mi") or unit.lower() == "m":
            key = "m"
        total += int(n) * _UP_MULT[key]
        hit = True
    return total if hit else None


# ---- writing ----

def record(samples: list) -> None:
    """samples: [(hotspot_id, online, away, temp_c, cpu, uptime_s)]"""
    if not samples:
        return
    now = int(time.time())
    try:
        with _lock:
            c = _conn()
            try:
                c.executemany(
                    "INSERT INTO samples (hotspot_id, ts, online, away, temp_c, cpu, uptime_s) VALUES (?,?,?,?,?,?,?)",
                    [(h, now, int(bool(o)), int(bool(a)), t, cp, u) for h, o, a, t, cp, u in samples])
                c.execute("DELETE FROM samples WHERE ts < ?", (now - (RETENTION_DAYS + 1) * 86400,))
                c.commit()
            finally:
                c.close()
    except Exception as e:
        print(f"[health] record failed: {type(e).__name__}: {e}", flush=True)


def sample_from_snapshot(hotspots: list, snap: dict) -> list:
    """Build one sample per enabled hotspot from monitor.snapshot()."""
    out = []
    for h in hotspots:
        if not h.get("enabled", True):
            continue
        s = snap.get(h.get("id"))
        if not s:
            continue
        online = s.get("status") not in ("Offline", None)
        away = (not online) and bool(h.get("portable"))
        temp = cpu = up = None
        if online:
            temp = parse_temp_c(s.get("temperature"))
            if temp is None and s.get("battery_cpu_temp_c") is not None:
                temp = float(s["battery_cpu_temp_c"])
            cpu = parse_pct(s.get("cpu"))
            up = parse_uptime(s.get("uptime"))
        out.append((h["id"], online, away, temp, cpu, up))
    return out


# ---- reading ----

def _rows(c, hotspot_id, since):
    return c.execute(
        "SELECT ts, online, away, temp_c, cpu, uptime_s FROM samples WHERE hotspot_id = ? AND ts >= ? ORDER BY ts",
        (hotspot_id, since)).fetchall()


def _events(rows, warm_c):
    """Outages, reboots and warm periods from an ordered sample list."""
    ev = []
    off_start = None
    warm_start = warm_peak = None
    last_on = None   # (ts, uptime_s) of the last online sample with an uptime
    for ts, online, away, temp, cpu, up in rows:
        # outages (away never counts)
        if not online and not away:
            if off_start is None:
                off_start = ts
        elif off_start is not None:
            ev.append({"kind": "offline", "at": off_start, "dur": ts - off_start})
            off_start = None
        # reboots: uptime should have grown by the time since the last online
        # sample; if it's clearly less, the hotspot restarted in between --
        # whether or not it was ever seen offline
        if online and up is not None:
            if last_on and up + 120 < last_on[1] + (ts - last_on[0]):
                ev.append({"kind": "reboot", "at": ts - up, "dur": None})
            last_on = (ts, up)
        # warm
        if temp is not None and temp >= warm_c:
            if warm_start is None:
                warm_start, warm_peak = ts, temp
            warm_peak = max(warm_peak, temp)
        elif warm_start is not None and (temp is not None or not online):
            ev.append({"kind": "warm", "at": warm_start, "dur": ts - warm_start, "peak_c": warm_peak})
            warm_start = None
    last_ts = rows[-1][0] if rows else None
    if off_start is not None:
        ev.append({"kind": "offline", "at": off_start, "dur": last_ts - off_start + SAMPLE_SEC, "ongoing": True})
    if warm_start is not None:
        ev.append({"kind": "warm", "at": warm_start, "dur": last_ts - warm_start + SAMPLE_SEC, "peak_c": warm_peak, "ongoing": True})
    ev.sort(key=lambda e: e["at"], reverse=True)
    return ev


def _buckets(rows, start, now, bucket_sec):
    n = max(1, int((now - start) // bucket_sec) + 1)
    b = [{"on": 0, "off": 0, "away": 0, "t": [], "cpu": []} for _ in range(n)]
    for ts, online, away, temp, cpu, up in rows:
        i = int((ts - start) // bucket_sec)
        if not 0 <= i < n:
            continue
        k = "on" if online else ("away" if away else "off")
        b[i][k] += 1
        if temp is not None:
            b[i]["t"].append(temp)
        if cpu is not None:
            b[i]["cpu"].append(cpu)
    out = []
    for x in b:
        state = "off" if x["off"] else ("away" if x["away"] and not x["on"] else ("on" if x["on"] else "none"))
        out.append({
            "s": state,
            "t": round(sum(x["t"]) / len(x["t"]), 1) if x["t"] else None,
            "tmax": max(x["t"]) if x["t"] else None,
            "cpu": round(sum(x["cpu"]) / len(x["cpu"]), 1) if x["cpu"] else None,
        })
    return out


def summary(hotspot_ids: list, days: int = 7, warm_c: float = 70.0, detail: bool = False) -> dict:
    """Per-hotspot health over the trailing `days` (max RETENTION_DAYS).
    Card/drawer strip is hourly for <= 7 days, 4-hourly beyond. `detail`
    adds the full event list and the chart series."""
    days = max(1, min(int(days), RETENTION_DAYS))
    now = int(time.time())
    start = now - days * 86400
    bucket = 3600 if days <= 7 else 4 * 3600
    out = {}
    try:
        with _lock:
            c = _conn()
            try:
                oldest = c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
                for hid in hotspot_ids:
                    rows = _rows(c, hid, start - 7 * 86400)   # one extra week for "vs last week"
                    cur = [r for r in rows if r[0] >= start]
                    prev_wk = [r for r in rows if now - 14 * 86400 <= r[0] < now - 7 * 86400]
                    this_wk = [r for r in rows if r[0] >= now - 7 * 86400]
                    on = sum(1 for r in cur if r[1]); off = sum(1 for r in cur if not r[1] and not r[2])
                    away = sum(1 for r in cur if not r[1] and r[2])
                    ev = _events(cur, warm_c)
                    avg = lambda rs: (sum(r[3] for r in rs if r[3] is not None) / max(1, sum(1 for r in rs if r[3] is not None))) if any(r[3] is not None for r in rs) else None
                    a_now, a_prev = avg(this_wk), avg(prev_wk)
                    last = cur[-1] if cur else None
                    spark = [x["t"] for x in _buckets([r for r in cur if r[0] >= now - 86400], now - 86400, now, 1800)]
                    entry = {
                        "samples": len(cur),
                        "uptime_pct": round(on / (on + off) * 100, 1) if (on + off) else None,
                        "away_hours": round(away * SAMPLE_SEC / 3600, 1),
                        "strip": [x["s"] for x in _buckets(cur, start, now, bucket)],
                        "bucket_sec": bucket,
                        "temp_c": last[3] if last else None,
                        "cpu": last[4] if last else None,
                        "uptime_s": last[5] if last else None,
                        "online": bool(last[1]) if last else None,
                        "spark": spark,
                        "week_delta_c": round(a_now - a_prev, 1) if (a_now is not None and a_prev is not None and len(prev_wk) >= 288) else None,
                        "outages": sum(1 for e in ev if e["kind"] == "offline"),
                        "outage_sec": sum(e["dur"] for e in ev if e["kind"] == "offline"),
                        "reboots": sum(1 for e in ev if e["kind"] == "reboot"),
                        "warm_periods": sum(1 for e in ev if e["kind"] == "warm"),
                    }
                    if detail:
                        entry["events"] = ev[:100]
                        entry["series"] = _buckets(cur, start, now, bucket if days > 7 else 3600)
                    out[hid] = entry
            finally:
                c.close()
    except Exception as e:
        print(f"[health] summary failed: {type(e).__name__}: {e}", flush=True)
        return {"hotspots": {}, "since": None, "now": now}
    return {"hotspots": out, "order": list(hotspot_ids), "since": oldest, "now": now, "days": days, "warm_c": warm_c}


class WarmWatcher:
    """Turns temperature samples into "running warm" / "cooled down" events
    for the hotspot notifications -- a transition, not a state (same rule as
    every other alert source): warm after 2 samples in a row at/above the
    threshold, cool once back 3 °C below it."""

    def __init__(self):
        self._warm = {}     # hotspot_id -> bool
        self._streak = {}   # hotspot_id -> consecutive hot samples

    def seed(self, hotspot_id, is_warm: bool):
        self._warm[hotspot_id] = is_warm

    def update(self, samples: list, warm_c: float) -> list:
        """samples as for record(); returns [(hotspot_id, "warm"|"cool", temp_c)]"""
        out = []
        for hid, online, away, temp, cpu, up in samples:
            if temp is None:
                continue
            if temp >= warm_c:
                self._streak[hid] = self._streak.get(hid, 0) + 1
                if self._streak[hid] >= 2 and not self._warm.get(hid):
                    self._warm[hid] = True
                    out.append((hid, "warm", temp))
            else:
                self._streak[hid] = 0
                if self._warm.get(hid) and temp <= warm_c - 3:
                    self._warm[hid] = False
                    out.append((hid, "cool", temp))
        return out
