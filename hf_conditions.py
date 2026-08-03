"""Live HF propagation/solar conditions from N0NBH's free public feed
(hamqsl.com/solarxml.php) -- no API key, no signup. See README.md "HF
Conditions card" for what each field means.

Cached for CACHE_TTL (1 hour) rather than polled per page load -- the
feed itself only updates on a similar cadence, so anything shorter is
just extra load on someone else's free service for no fresher data.
Degrades the same way every other integration in this app does: any
fetch/parse failure returns None (or the last good cache, if there is
one) rather than raising, so a flaky feed never breaks the dashboard.
"""
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque

import storage_notifications

FEED_URL  = "https://www.hamqsl.com/solarxml.php"
CACHE_TTL = 3600
TIMEOUT   = 10
AGENT     = "hotspot-dashboard/1.0"

# NOAA's G-scale calls Kp>=5 a G1 "minor" geomagnetic storm -- the
# conventional threshold ham operators watch for (aurora propagation
# possible, HF absorption/degradation likely). Used for the Notifications
# card's solar-alert feed, not for anything on the HF Conditions card
# itself, which just shows the raw value with no judgment applied.
KP_ALERT_THRESHOLD = 5.0

# Display order -- the feed's own <band> element order isn't guaranteed,
# so bands are collected into a dict first and re-ordered against this.
BAND_ORDER = ["80m-40m", "30m-20m", "17m-15m", "12m-10m"]


class HfConditionsClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = 0.0
        # Geomagnetic-storm alert log for the Notifications card -- fires
        # only on a threshold CROSSING (like monitor.py's fleet online/
        # offline events), not every hourly refresh while K-index stays
        # elevated, so a multi-hour storm doesn't spam the same alert
        # over and over.
        self._alert_events: deque = deque(maxlen=50)
        # Reseed from disk so a restart doesn't lose alert history --
        # appends oldest-to-newest (see _check_alert_transition), so
        # replaying the oldest-first persisted list through the same
        # .append() reproduces live insertion order.
        for payload in storage_notifications.recent("solar", self._alert_events.maxlen):
            self._alert_events.append(payload)

    def get(self, force: bool = False) -> dict | None:
        with self._lock:
            if not force and self._cache is not None and (time.time() - self._cache_time) < CACHE_TTL:
                return self._cache
            previous = self._cache

        result = self._fetch()
        with self._lock:
            if result is not None:
                self._check_alert_transition(previous, result)
                self._cache = result
                self._cache_time = time.time()
            return self._cache

    def alert_events(self) -> list:
        """Recent geomagnetic-storm alert transitions, newest first, for
        the Notifications card."""
        with self._lock:
            return list(reversed(self._alert_events))

    def _check_alert_transition(self, previous: dict | None, result: dict) -> None:
        """Caller must already hold self._lock. No-ops on the very first
        fetch ever (no prior K-index to compare against, so no
        transition can be detected) -- same "own real data, not a
        fabricated baseline" reasoning as every other integration here."""
        if previous is None:
            return
        try:
            old_k = float(previous.get("k_index"))
            new_k = float(result.get("k_index"))
        except (TypeError, ValueError):
            return
        was_elevated = old_k >= KP_ALERT_THRESHOLD
        is_elevated = new_k >= KP_ALERT_THRESHOLD
        if is_elevated and not was_elevated:
            event = {"kind": "elevated", "k_index": new_k, "at": time.time()}
            self._alert_events.append(event)
            storage_notifications.log_notification("solar", event["at"], event, self._alert_events.maxlen)
        elif was_elevated and not is_elevated:
            event = {"kind": "normal", "k_index": new_k, "at": time.time()}
            self._alert_events.append(event)
            storage_notifications.log_notification("solar", event["at"], event, self._alert_events.maxlen)

    @staticmethod
    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(FEED_URL, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            root = ET.fromstring(data)
            solar = root.find("solardata")
            if solar is None:
                return None

            def text(tag):
                el = solar.find(tag)
                if el is None or el.text is None:
                    return None
                val = el.text.strip()
                return val or None

            band_map = {}
            calc = solar.find("calculatedconditions")
            if calc is not None:
                for band in calc.findall("band"):
                    name = band.get("name")
                    tod  = band.get("time")
                    if not name or not tod:
                        continue
                    band_map.setdefault(name, {})[tod] = (band.text or "").strip() or None

            bands = [
                {"name": name, "day": band_map[name].get("day"), "night": band_map[name].get("night")}
                for name in BAND_ORDER if name in band_map
            ]

            return {
                "updated":      text("updated"),
                "solar_flux":   text("solarflux"),
                "a_index":      text("aindex"),
                "k_index":      text("kindex"),
                "sunspots":     text("sunspots"),
                "xray":         text("xray"),
                "aurora":       text("aurora"),
                "geomag_field": text("geomagfield"),
                "signal_noise": text("signalnoise"),
                "bands":        bands,
                "fetched_at":   time.time(),
            }
        except Exception:
            return None
