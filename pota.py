"""Live Parks on the Air (POTA) activator spots for the Live map's
optional "POTA spots" overlay.

https://api.pota.app/spot/activator is a free, no-auth, CORS-open
(`Access-Control-Allow-Origin: *`) public endpoint -- confirmed live
before building this, not from POTA's own docs, which are explicitly
"under construction" (docs.pota.app). No documented rate limit, but
cached server-side like every other integration here rather than
assumed unlimited. Returns the FULL current spot list directly, no query
params needed -- each spot already carries its own expiry, so unlike
wspr.live's WSPR spots (removed from this app for being visual clutter),
POTA's spot count is naturally small and self-bounding: an "activation"
is a multi-hour event, not a per-decode/per-second signal report.

Confirmed live field shape (a real response, not assumed):
{"activator": "KA0CSW", "frequency": "7059.9", "mode": "CW",
 "reference": "US-0362", "parkName": null, "name": "<full park name>",
 "spotTime": "2026-07-23T00:36:06", "latitude": 45.0718,
 "longitude": -94.5328, ...}
`frequency` is kHz as a string (e.g. "7059.9" = 7.0599 MHz) -- converted
to Hz here to reuse wsjtx.py's freq_to_band(), rather than a third
duplicate band-edge table (wspr_activity.py and wsjtx.py each already
have one, in different shapes for their own historical reasons).
"""
import json
import threading
import time
import urllib.request

from wsjtx import freq_to_band

POTA_SPOTS_URL = "https://api.pota.app/spot/activator"
CACHE_TTL = 20  # seconds -- a hunter chasing a live activator cares about
                # freshness (an activator can QSY or finish up within a
                # couple minutes, so a stale spot list means chasing
                # someone who's already gone). Bumped up from an initial
                # 60s specifically for this reason -- still nowhere near
                # aggressive for a CORS-open, rate-limit-undocumented
                # public endpoint that didn't push back at all in testing.
TIMEOUT = 15
AGENT = "hotspot-dashboard/1.0"


class PotaClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = 0.0

    def get(self, force: bool = False) -> dict | None:
        with self._lock:
            fresh = (
                not force and self._cache is not None
                and (time.time() - self._cache_time) < CACHE_TTL
            )
            if fresh:
                return self._cache

        result = self._fetch()
        with self._lock:
            if result is not None:
                self._cache      = result
                self._cache_time = time.time()
                return self._cache
            # Degrade gracefully like every other client here -- serve the
            # last good snapshot rather than blanking the layer.
            return self._cache

    @staticmethod
    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(POTA_SPOTS_URL, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                rows = json.loads(resp.read())
        except Exception:
            return None
        if not isinstance(rows, list):
            return None

        spots = []
        for r in rows:
            lat, lon = r.get("latitude"), r.get("longitude")
            if lat is None or lon is None:
                continue  # can't plot without a position
            try:
                freq_hz = int(float(r["frequency"]) * 1000)
            except (KeyError, TypeError, ValueError):
                freq_hz = None
            spots.append({
                "activator":  r.get("activator"),
                "reference":  r.get("reference"),
                "park_name":  r.get("parkName") or r.get("name"),
                "mode":       (r.get("mode") or "").strip().upper(),
                "band":       freq_to_band(freq_hz) if freq_hz else "",
                "lat": lat, "lon": lon,
                "spot_time":  r.get("spotTime"),
            })
        return {"spots": spots, "fetched_at": time.time()}
