"""Satellite pass alerts -- a Notifications source (and optional Pocket Dash
push) that fires once per pass, a few minutes before a tracked satellite
rises, for passes that climb high enough to be worth working.

Built entirely on satellites.py's existing pass predictions (the same ones
the Satellites card shows): no new network source. app.py runs tick() once
a minute from its own background thread.

Rules (all from settings.json):
  sat_alerts_enabled      master switch
  sat_alert_lead_min      how many minutes before AOS to alert (default 10)
  sat_alert_min_el        only passes whose max elevation reaches this (30)
  tracked_satellites[].alert   per satellite, default True
  sat_alert_quiet / _quiet_start / _quiet_end
                          skip alerts (card AND phone) between these hours
  station_tz              IANA zone the quiet hours are read in -- the
                          Settings page fills it in from the browser, since
                          a Docker container's clock is usually UTC

Once per pass: each pass is keyed by satellite + AOS minute, and a new
prediction whose AOS lands within DEDUPE_WINDOW_SEC of an already-alerted
pass for the same satellite counts as the same pass (predictions step in
30 s, so AOS can shift slightly between refreshes). Alerted keys are
reseeded from the persisted events on restart, and a pass skipped for quiet
hours is remembered too, so it never fires late at 06:00.

Never raises out of tick()/status() -- same contract as every integration.
"""
import datetime
import threading
import time
from collections import deque

import storage_notifications

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover -- Python < 3.9
    ZoneInfo = None

MAX_EVENTS = 50
DEDUPE_WINDOW_SEC = 300


def zone(name):
    """ZoneInfo for an IANA name, or None when unknown/unavailable."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def in_quiet_hours(settings: dict, now: float) -> bool:
    if not settings.get("sat_alert_quiet", True):
        return False
    try:
        start = int(settings.get("sat_alert_quiet_start", 23)) % 24
        end = int(settings.get("sat_alert_quiet_end", 6)) % 24
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    tz = zone(settings.get("station_tz")) or datetime.timezone.utc
    hour = datetime.datetime.fromtimestamp(now, tz).hour
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


class SatPassAlerter:
    def __init__(self):
        self._lock = threading.Lock()
        self._events = deque(maxlen=MAX_EVENTS)   # oldest first; read newest first
        self._alerted = {}                        # norad_id -> [aos_epoch, ...]
        self._last_error = None
        for ev in storage_notifications.recent("sat", MAX_EVENTS):
            self._events.append(ev)
            self._remember(ev.get("norad_id"), ev.get("aos_epoch"))

    def _remember(self, norad_id, aos):
        if norad_id is None or aos is None:
            return
        lst = self._alerted.setdefault(norad_id, [])
        lst.append(aos)
        cutoff = time.time() - 86400
        self._alerted[norad_id] = [a for a in lst if a > cutoff]

    def _seen(self, norad_id, aos) -> bool:
        return any(abs(a - aos) <= DEDUPE_WINDOW_SEC for a in self._alerted.get(norad_id, []))

    def tick(self, settings: dict, tracker, observer) -> list:
        """One pass over upcoming passes. Returns the NEW events raised this
        tick (so the caller can push them), [] on anything unusual."""
        if not settings.get("sat_alerts_enabled") or observer is None:
            return []
        try:
            tracked = [s for s in (settings.get("tracked_satellites") or []) if s.get("alert", True)]
            if not tracked:
                return []
            lead = max(1, min(120, int(settings.get("sat_alert_lead_min", 10)))) * 60
            min_el = float(settings.get("sat_alert_min_el", 30))
            now = time.time()
            quiet = in_quiet_hours(settings, now)
            new = []
            for p in tracker.passes(tracked, observer[0], observer[1]):
                aos = p.get("aos_epoch")
                if aos is None or not (now <= aos <= now + lead):
                    continue
                if (p.get("max_elevation") or 0) < min_el:
                    continue
                with self._lock:
                    if self._seen(p.get("norad_id"), aos):
                        continue
                    self._remember(p.get("norad_id"), aos)
                    if quiet:
                        continue
                    ev = {
                        "at": now,
                        "norad_id": p.get("norad_id"),
                        "name": p.get("name"),
                        "mode": p.get("mode"),
                        "downlink_mhz": p.get("downlink_mhz"),
                        "uplink_mhz": p.get("uplink_mhz"),
                        "aos_epoch": aos,
                        "los_epoch": p.get("los_epoch"),
                        "max_elevation": p.get("max_elevation"),
                        "aos_azimuth": p.get("aos_azimuth"),
                        "los_azimuth": p.get("los_azimuth"),
                    }
                    self._events.append(ev)
                storage_notifications.log_notification("sat", now, ev, MAX_EVENTS)
                new.append(ev)
            self._last_error = None
            return new
        except Exception as e:  # never let a bad prediction kill the loop
            self._last_error = f"{type(e).__name__}: {e}"
            return []

    def status(self, settings: dict, has_location: bool) -> dict:
        with self._lock:
            return {
                "enabled": bool(settings.get("sat_alerts_enabled")),
                "connected": has_location,
                "error": self._last_error,
                "events": list(self._events)[::-1],
            }


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(az) -> str:
    try:
        return COMPASS[int(round(float(az) / 22.5)) % 16]
    except (TypeError, ValueError):
        return ""


def push_text(ev: dict, now: float) -> tuple:
    """(title, body) for the phone notification."""
    mins = max(0, round((ev["aos_epoch"] - now) / 60))
    title = f"\U0001F6F0 {ev.get('name') or 'Satellite'} rises in {mins} min"
    bits = []
    if ev.get("max_elevation") is not None:
        bits.append(f"Max {round(ev['max_elevation'])}°")
    a, b = compass(ev.get("aos_azimuth")), compass(ev.get("los_azimuth"))
    if a and b:
        bits.append(f"{a} → {b}")
    if ev.get("downlink_mhz"):
        bits.append(f"{ev['downlink_mhz']:.3f} {ev.get('mode') or ''}".strip())
    return title, " · ".join(bits)
