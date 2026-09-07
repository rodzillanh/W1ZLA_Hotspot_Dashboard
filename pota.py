"""Parks on the Air (POTA) -- live activator spots for the Live map's
optional "POTA spots" overlay, and (v4.47) the POTA card's ranked spot
list + hunter stats.

https://api.pota.app is free, no-auth, CORS-open
(`Access-Control-Allow-Origin: *`) -- confirmed live before building
this, not from POTA's own docs, which are explicitly "under
construction" (docs.pota.app). No documented rate limit, but cached
server-side like every other integration here rather than assumed
unlimited.

Endpoints used, all confirmed live against a real response:
  * /spot/activator          -- the FULL current spot list, no params.
  * /profile/<callsign>       -- a hunter/activator profile: nested
                                `stats` (hunter parks/qsos, awards,
                                endorsements, activator activations) plus
                                `recent_activity.hunter_qsos` (last ~10,
                                each with a park `reference`). One call
                                gives everything the card's drawer needs;
                                `/stats/user/<callsign>` returns the same
                                `stats` block without the recent list.

`spot.frequency` is kHz as a string ("7059.9" = 7.0599 MHz) -- converted
to Hz here to reuse wsjtx.py's freq_to_band(), rather than a third
duplicate band-edge table.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

from wsjtx import freq_to_band

SPOTS_URL   = "https://api.pota.app/spot/activator"
PROFILE_URL = "https://api.pota.app/profile/"
SPOTS_TTL   = 20   # seconds -- a hunter chasing a live activator cares
                   # about freshness (an activator can QSY or finish up
                   # within a couple minutes). Nowhere near aggressive for
                   # a CORS-open, rate-limit-undocumented public endpoint.
HUNTER_TTL  = 600  # 10 min -- your own totals barely move minute to
                   # minute, and a confirmed hunt shows up in
                   # recent_activity on POTA's side on its own schedule.
TIMEOUT = 15
AGENT = "hotspot-dashboard/1.0"


class PotaClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = 0.0
        # hunter profiles cached per uppercased callsign
        self._hlock = threading.Lock()
        self._hcache: dict = {}  # call -> (expiry_epoch, result|None)

    # --- spots (Live map overlay + the POTA card body) ---

    def get(self, force: bool = False) -> dict | None:
        with self._lock:
            fresh = (
                not force and self._cache is not None
                and (time.time() - self._cache_time) < SPOTS_TTL
            )
            if fresh:
                return self._cache

        result = self._fetch_spots()
        with self._lock:
            if result is not None:
                self._cache = result
                self._cache_time = time.time()
                return self._cache
            # Degrade gracefully -- serve the last good snapshot rather
            # than blanking the layer.
            return self._cache

    # --- hunter profile (POTA card drawer) ---

    def hunter(self, callsign: str) -> dict | None:
        call = (callsign or "").strip().upper()
        if not call:
            return None
        with self._hlock:
            cached = self._hcache.get(call)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._fetch_profile(call)
        with self._hlock:
            # Cache a good result for HUNTER_TTL; on failure, keep serving
            # the last good one (with its old expiry) if we have it.
            if result is not None:
                self._hcache[call] = (time.time() + HUNTER_TTL, result)
                return result
            return cached[1] if cached else None

    # --- internals ---

    @staticmethod
    def _fetch_spots() -> dict | None:
        try:
            req = urllib.request.Request(SPOTS_URL, headers={"User-Agent": AGENT})
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
                continue  # can't plot / distance-rank without a position
            try:
                freq_hz = int(float(r["frequency"]) * 1000)
            except (KeyError, TypeError, ValueError):
                freq_hz = None
            spots.append({
                "activator":     r.get("activator"),
                "reference":     r.get("reference"),
                "park_name":     r.get("parkName") or r.get("name"),
                "mode":          (r.get("mode") or "").strip().upper(),
                "band":          freq_to_band(freq_hz) if freq_hz else "",
                "freq_hz":       freq_hz,
                "lat": lat, "lon": lon,
                "spot_time":     r.get("spotTime"),
                "qso_count":     r.get("count"),
                "comments":      (r.get("comments") or "").strip() or None,
                "spotter":       r.get("spotter"),
                "location_desc": r.get("locationDesc"),
            })
        return {"spots": spots, "fetched_at": time.time()}

    @staticmethod
    def _fetch_profile(call: str) -> dict | None:
        try:
            req = urllib.request.Request(
                PROFILE_URL + urllib.parse.quote(call, safe=""),
                headers={"User-Agent": AGENT},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read())
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        stats = data.get("stats") or {}
        hunter = stats.get("hunter") or {}
        activator = stats.get("activator") or {}
        attempts = stats.get("attempts") or {}
        recent = []
        for h in ((data.get("recent_activity") or {}).get("hunter_qsos") or []):
            recent.append({
                "date":      h.get("date"),
                "callsign":  h.get("callsign"),
                "reference": h.get("reference"),
                "park":      h.get("park"),
                "band":      h.get("band"),
                "mode":      h.get("mode"),
                "location":  h.get("location"),
            })
        return {
            "callsign":    data.get("callsign") or call,
            "name":        data.get("name") or None,
            "parks":       hunter.get("parks"),
            "qsos":        hunter.get("qsos"),
            "awards":      stats.get("awards"),
            "endorsements": stats.get("endorsements"),
            "activations": activator.get("activations"),
            "attempt_activations": attempts.get("activations"),
            "is_activator": bool(activator.get("activations")),
            "recent_hunts": recent,
        }
