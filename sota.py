"""Live SOTA (Summits on the Air) activator spots for the Live map's
optional "SOTA spots" overlay -- api2.sota.org.uk (free, no key,
confirmed live: a real curl returns real, current spots with fields
including callsign/associationCode/summitCode/frequency/mode/comments/
summitDetails/timeStamp).

Unlike pota.py (POTA's own feed already carries lat/lon directly), SOTA's
spot feed carries only an association+summit code, no coordinates at
all -- a SECOND lookup per distinct summit against
api-db2.sota.org.uk/api/summits/{assoc}/{code} is needed, also confirmed
live (returns "latitude"/"longitude" directly). A summit's location never
changes, so that lookup is cached indefinitely per summit code rather
than re-fetched on every spot-list poll like the spots themselves.

timeStamp arrives with no timezone suffix (e.g. "2026-07-31T20:57:01") --
confirmed to be UTC by comparing a live spot's timestamp against
wall-clock UTC "now" at fetch time (same discipline as
psk_reporter.py's flowStartSeconds verification), not assumed from the
field name alone.
"""
import time
import threading
import datetime
import urllib.request
import json

SPOTS_URL = "https://api2.sota.org.uk/api/spots/-1/all"
SUMMIT_URL_TMPL = "https://api-db2.sota.org.uk/api/summits/{assoc}/{code}"
CACHE_TTL = 120  # spots list -- same cadence class as pota.py
TIMEOUT = 10
AGENT = "hotspot-dashboard/1.0"
MAX_SPOT_AGE_MIN = 60  # only recent activator activity, same spirit as POTA/PSK overlays


def _parse_utc(ts: str) -> "float | None":
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _summit_short_name(details: "str | None") -> "str | None":
    """summitDetails arrives as one combined string, e.g.
    "Bear Benchmark, 2860m, 8 points" -- just the name is wanted here,
    elevation/points aren't shown elsewhere in this app's spot overlays
    (POTA's own overlay doesn't show park size/rules either)."""
    if not details:
        return None
    return details.split(",")[0].strip() or None


class SotaClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: "dict | None" = None
        self._cache_time = 0.0
        self._summit_cache: dict = {}  # "ASSOC/CODE" -> (lat, lon), successful lookups only

    def get(self) -> "dict | None":
        with self._lock:
            if self._cache is not None and (time.time() - self._cache_time) < CACHE_TTL:
                return self._cache

        result = self._fetch()
        with self._lock:
            if result is not None:
                self._cache = result
                self._cache_time = time.time()
            return self._cache

    def _fetch(self) -> "dict | None":
        try:
            req = urllib.request.Request(SPOTS_URL, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = json.loads(resp.read())
        except Exception:
            return None

        cutoff = time.time() - MAX_SPOT_AGE_MIN * 60
        spots = []
        for s in raw:
            try:
                epoch = _parse_utc(s.get("timeStamp"))
                if epoch is None or epoch < cutoff:
                    continue
                assoc = (s.get("associationCode") or "").strip()
                code = (s.get("summitCode") or "").strip()
                if not assoc or not code:
                    continue
                latlon = self._summit_latlon(assoc, code)
                if latlon is None:
                    continue
                lat, lon = latlon
                spots.append({
                    "callsign": s.get("activatorCallsign") or s.get("callsign"),
                    "summit_code": f"{assoc}/{code}",
                    "summit_name": _summit_short_name(s.get("summitDetails")),
                    "frequency": s.get("frequency"),
                    "mode": s.get("mode"),
                    "comment": s.get("comments"),
                    "lat": lat, "lon": lon,
                    "spotted_at": epoch,
                })
            except Exception:
                continue  # one malformed spot shouldn't drop the whole feed
        return {"spots": spots, "fetched_at": time.time()}

    def _summit_latlon(self, assoc: str, code: str) -> "tuple | None":
        key = f"{assoc}/{code}"
        with self._lock:
            cached = self._summit_cache.get(key)
        if cached is not None:
            return cached
        result = self._fetch_summit_latlon(assoc, code)
        if result is not None:
            with self._lock:
                self._summit_cache[key] = result
        return result

    @staticmethod
    def _fetch_summit_latlon(assoc: str, code: str) -> "tuple | None":
        try:
            url = SUMMIT_URL_TMPL.format(assoc=assoc, code=code)
            req = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read())
            lat, lon = data.get("latitude"), data.get("longitude")
            if lat is None or lon is None:
                return None
            return (float(lat), float(lon))
        except Exception:
            return None
