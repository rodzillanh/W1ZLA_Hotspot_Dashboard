"""Live NOAA SWPC auroral-oval intensity overlay for the Live map's
optional "Aurora oval" layer -- a different kind of data than
hf_conditions.py's `aurora` field (N0NBH's single 0-9 activity number);
this is the actual OVATION model grid.

Feed shape confirmed live via a real full download (not just docs, and
not just a partial/truncated fetch -- an initial WebFetch-based check
gave inconsistent partial numbers on this same feed, so the real values
below come from a direct curl + full JSON parse):

    {"Observation Time": "...", "Forecast Time": "...",
     "Data Format": "[Longitude, Latitude, Aurora]",
     "coordinates": [[lon, lat, value], ...], "type": "..."}

- 65,160 total coordinate entries -- exactly 360 x 181, a full 1x1
  degree global grid.
- Longitude is 0-359 (NOT -180..180) -- normalized in _fetch() below.
- Latitude is -90..90 standard.
- Value range at verification time (quiet conditions): 0-18, with only
  ~1000 points above 10 and none above 30 -- expected to range higher
  during a real geomagnetic storm, but the distribution is naturally
  top-heavy-sparse (most of the grid is background/zero at any time),
  so a floor filter (MIN_INTENSITY) stays cheap regardless of activity
  level.

NOAA's CDN sets Cache-Control: max-age=60, but the underlying OVATION
model itself is not believed to update anywhere near that often --
CACHE_TTL here is chosen to respect the model's real cadence, not the
edge-cache figure, same reasoning hf_conditions.py's own CACHE_TTL uses.
"""
import time
import threading
import urllib.request
import json

FEED_URL      = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
CACHE_TTL     = 300   # 5 min
TIMEOUT       = 10
AGENT         = "hotspot-dashboard/1.0"
MIN_INTENSITY = 10    # confirmed reasonable against real live data -- see module docstring


class AuroraClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = 0.0

    def get(self, force: bool = False) -> dict | None:
        with self._lock:
            if not force and self._cache is not None and (time.time() - self._cache_time) < CACHE_TTL:
                return self._cache

        result = self._fetch()
        with self._lock:
            if result is not None:
                self._cache      = result
                self._cache_time = time.time()
            return self._cache

    @staticmethod
    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(FEED_URL, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read())

            points = payload.get("coordinates", [])
            filtered = []
            for lon, lat, value in points:
                if value is None or value < MIN_INTENSITY:
                    continue
                if lon > 180:
                    lon -= 360
                filtered.append({"lat": lat, "lon": lon, "value": value})

            return {
                "points":      filtered,
                "observed_at": payload.get("Observation Time"),
                "fetched_at":  time.time(),
            }
        except Exception:
            return None
