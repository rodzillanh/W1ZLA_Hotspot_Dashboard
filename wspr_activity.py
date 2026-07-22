"""Live WSPR beacon-spot activity for the Band Activity card -- a
DIFFERENT kind of data than hf_conditions.py's N0NBH feed: this is real
observed spot counts (wspr.live), not a solar-index prediction. See
README.md "Band Activity card" for the distinction.

wspr.live (https://wspr.live/) is a free, documented, no-auth public
ClickHouse HTTP query API over the WSPRnet spot database -- confirmed
live (not just from docs) before building this: verified the exact band
code for each ham band via a real query (band is NOT simply "MHz digit"
for the low bands -- 160m is band=1, not 1.8), verified the geoDistance()
SQL function works for radius filtering, and verified a Maidenhead grid
square converts to the same lat/lon wspr.live itself reports for a known
real station (grid "JN58th" -> 48.3125,11.625, an exact match to that
row's own tx_lat/tx_lon). Rate-limited by wspr.live to 20 req/min;
non-commercial use only per their terms -- fine for a free self-hosted
dashboard, but don't repurpose this client for anything commercial.

Cached CACHE_TTL (30 min) per station_grid -- an hourly-bucketed chart
doesn't need to be re-fetched more often than that, and every re-fetch
counts against wspr.live's shared free rate limit.

WsprSpotsClient (below) is a second, unrelated client sharing this same
module/rate-limit budget -- global (not per-station_grid) individual
spot pairs for the Live map's optional "WSPR spots" overlay, not an
hourly-bucketed count. Its columns were confirmed live via `DESCRIBE
TABLE wspr.rx` -- the callsign columns are `tx_sign`/`rx_sign`, NOT
`tx_call`/`rx_call` as first guessed (a real 404/UNKNOWN_IDENTIFIER
error caught this before it shipped). Spot arrival is bursty, not a
smooth stream: a live check found a 120-second window sometimes returns
zero rows, while a 5-minute window reliably returned ~6350 globally and
a 10-minute window ~22855 -- so this client queries a wider 5-minute
window and relies on ORDER BY ... LIMIT to cap the result size, rather
than a tight time window to bound it.
"""
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

WSPR_LIVE_URL = "https://db1.wspr.live/"
CACHE_TTL     = 1800  # 30 min
TIMEOUT       = 15
AGENT         = "hotspot-dashboard/1.0"
RADIUS_METERS = 500_000  # 500km -- see README.md for why this radius

# WSPR band codes -- confirmed live against wspr.live (NOT "first digit
# of frequency in MHz" for bands below 10 MHz; e.g. 160m is band=1, not
# 1.8). Grouped to match hf_conditions.py's BAND_ORDER pairs, so the two
# cards read as companions: same band groupings, two different kinds of
# data. 60m (band=5) has no home in that scheme and is deliberately
# left out rather than force-fit into 80m-40m.
BAND_GROUPS = [
    ("160m", [1]),
    ("80m-40m", [3, 7]),
    ("30m-20m", [10, 14]),
    ("17m-15m", [18, 21]),
    ("12m-10m", [24, 28]),
    ("6m", [50]),
]
_ALL_BANDS = [b for _, bands in BAND_GROUPS for b in bands]


def grid_to_latlon(grid: str) -> tuple[float, float] | None:
    """Convert a 4- or 6-character Maidenhead grid square to the lat/lon
    of its center. Returns None if grid isn't a well-formed locator.
    Verified against a real wspr.live row: grid "JN58th" -> (48.3125,
    11.625), an exact match to that row's own tx_lat/tx_lon fields."""
    grid = grid.strip().upper()
    if len(grid) < 4 or not (grid[0].isalpha() and grid[1].isalpha()
                              and grid[2].isdigit() and grid[3].isdigit()):
        return None
    lon = (ord(grid[0]) - ord('A')) * 20 - 180
    lat = (ord(grid[1]) - ord('A')) * 10 - 90
    lon += int(grid[2]) * 2
    lat += int(grid[3])
    if len(grid) >= 6 and grid[4].isalpha() and grid[5].isalpha():
        lon += (ord(grid[4].lower()) - ord('a')) * (2 / 24)
        lat += (ord(grid[5].lower()) - ord('a')) * (1 / 24)
        lon += (2 / 24) / 2
        lat += (1 / 24) / 2
    else:
        lon += 1
        lat += 0.5
    return round(lat, 4), round(lon, 4)


class WsprActivityClient:
    def __init__(self):
        self._lock       = threading.Lock()
        self._cache      = None
        self._cache_grid = None
        self._cache_time = 0.0

    def get(self, station_grid: str, force: bool = False) -> dict | None:
        station_grid = (station_grid or "").strip().upper()
        if not station_grid:
            return None
        latlon = grid_to_latlon(station_grid)
        if latlon is None:
            return None

        with self._lock:
            fresh = (
                not force and self._cache is not None
                and self._cache_grid == station_grid
                and (time.time() - self._cache_time) < CACHE_TTL
            )
            if fresh:
                return self._cache

        result = self._fetch(latlon)
        if result is not None:
            result["station_grid"] = station_grid
            result["radius_km"]    = RADIUS_METERS // 1000
        with self._lock:
            if result is not None:
                self._cache      = result
                self._cache_grid = station_grid
                self._cache_time = time.time()
                return self._cache
            # Serve a stale cache for the same grid on a transient
            # failure rather than blanking the card -- same
            # degrade-gracefully pattern as every other integration.
            if self._cache_grid == station_grid:
                return self._cache
            return None

    @staticmethod
    def _fetch(latlon: tuple[float, float]) -> dict | None:
        lat, lon = latlon
        query = (
            "SELECT band, toStartOfHour(time) as hr, count() as cnt "
            "FROM wspr.rx "
            f"WHERE band IN ({','.join(str(b) for b in _ALL_BANDS)}) "
            "AND time > now() - INTERVAL 24 HOUR "
            f"AND geoDistance(rx_lon, rx_lat, {lon}, {lat}) < {RADIUS_METERS} "
            "GROUP BY band, hr ORDER BY band, hr FORMAT JSON"
        )
        url = WSPR_LIVE_URL + "?query=" + urllib.parse.quote_plus(query)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read())
            rows = payload.get("data", [])
        except Exception:
            return None
        if not rows:
            return None

        by_band: dict[int, dict[str, int]] = {}
        seen_hours = []
        for row in rows:
            by_band.setdefault(row["band"], {})[row["hr"]] = row["cnt"]
            seen_hours.append(row["hr"])
        if not seen_hours:
            return None

        # Explicit continuous hour range (not just "hours with any data")
        # so every group's array is the same length even if some hour had
        # a genuine zero across every band -- a real possibility on a
        # quiet band at a tight radius, not something to paper over.
        start = datetime.strptime(min(seen_hours), "%Y-%m-%d %H:%M:%S")
        end   = datetime.strptime(max(seen_hours), "%Y-%m-%d %H:%M:%S")
        hours = []
        h = start
        while h <= end:
            hours.append(h.strftime("%Y-%m-%d %H:%M:%S"))
            h += timedelta(hours=1)

        groups = []
        for label, bands in BAND_GROUPS:
            values = [sum(by_band.get(b, {}).get(h, 0) for b in bands) for h in hours]
            peak = max(values) if values else 0
            peak_idx = values.index(peak) if values else 0
            peak_hour = hours[peak_idx][-8:-3] if hours else None
            groups.append({
                "label": label,
                "hourly": values,
                "now": values[-1] if values else 0,
                "peak": peak,
                "peak_hour": peak_hour,
            })

        return {"groups": groups, "fetched_at": time.time()}


# Global (no station_grid/radius filter), individual spot pairs for the
# Live map's optional "WSPR spots" overlay -- deliberately worldwide, not
# local, so the layer is always populated and reads as "a lot happening
# right now" regardless of whether a station_grid is configured.
SPOT_CACHE_TTL  = 90    # seconds -- feels live without troubling the shared 20 req/min budget
SPOT_WINDOW_SEC = 300   # 5 min -- confirmed live that a tight 120s window can return zero
                        # rows (spot inserts arrive in bursts, not a smooth stream); 5 min
                        # reliably had ~6350 rows globally when checked live
SPOT_LIMIT      = 400   # caps pins/lines to something Leaflet renders smoothly


class WsprSpotsClient:
    """Live global WSPR spot pairs, refreshed independently of
    WsprActivityClient above (different cache key shape: this has no key
    at all, just one shared global snapshot)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = 0.0

    def get(self, force: bool = False) -> dict | None:
        with self._lock:
            fresh = (
                not force and self._cache is not None
                and (time.time() - self._cache_time) < SPOT_CACHE_TTL
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
        query = (
            "SELECT tx_sign, rx_sign, tx_lat, tx_lon, rx_lat, rx_lon, band, snr, time "
            "FROM wspr.rx "
            f"WHERE band IN ({','.join(str(b) for b in _ALL_BANDS)}) "
            f"AND time > now() - INTERVAL {SPOT_WINDOW_SEC} SECOND "
            f"ORDER BY time DESC LIMIT {SPOT_LIMIT} FORMAT JSON"
        )
        url = WSPR_LIVE_URL + "?query=" + urllib.parse.quote_plus(query)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read())
            rows = payload.get("data", [])
        except Exception:
            return None

        band_label = {b: label for label, bands in BAND_GROUPS for b in bands}
        spots = [{
            "tx_call": r["tx_sign"], "rx_call": r["rx_sign"],
            "tx_lat":  r["tx_lat"],  "tx_lon":  r["tx_lon"],
            "rx_lat":  r["rx_lat"],  "rx_lon":  r["rx_lon"],
            "band":    band_label.get(r["band"], str(r["band"])),
            "snr":     r.get("snr"),
        } for r in rows]
        return {"spots": spots, "fetched_at": time.time()}
