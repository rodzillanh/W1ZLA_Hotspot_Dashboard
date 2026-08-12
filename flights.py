"""Live nearby-aircraft feed for the Flights Overhead card -- OpenSky
Network's free, anonymous /api/states/all endpoint (confirmed live, no
auth required: a real anonymous bounding-box query against
opensky-network.org during development returned real state vectors --
same "verify against the real thing" discipline as every other
integration in this project, not assumed from their docs alone).

Real, disclosed data gap: OpenSky's live state vectors carry NO aircraft
type/model/registration and NO route (origin/destination) -- confirmed
by reading their actual REST API docs and a live test query, not
guessed. Aircraft type/registration only exists in OpenSky's own bulk
aircraftDatabase.csv download (~95MB, hundreds of thousands of rows) --
far too heavy to bundle/sync for this app's "one small self-contained
client" pattern (contrast extra_2024_2028.json, a few hundred KB).
Route data isn't in their REST API at all -- only in a separate
estimated-flights-by-time-range endpoint meant for historical lookups,
not "what's this aircraft's live route right now". So this deliberately
surfaces only what the live feed actually has: callsign, icao24,
position, altitude, speed, heading, vertical rate, squawk, and a coarse
ADS-B emitter category (Light/Large/Heavy/Rotorcraft/etc, straight from
the state vector's own `category` field) -- no invented aircraft type,
no invented route. The dashboard card links out to FlightAware for
anyone who wants the real type/route for a specific flight.

Anonymous access is capped at 400 "credits"/day; a bounding box this
small (~80km across) costs 1 credit/query (confirmed against OpenSky's
own published cost table) -- CACHE_TTL below keeps this well under
budget with real margin, same conservative-over-documented-minimum
posture as psk_reporter.py's 600s cache.
"""
import json
import math
import threading
import time
import urllib.parse
import urllib.request

from wspr_activity import grid_to_latlon

OPENSKY_URL = "https://opensky-network.org/api/states/all"
CACHE_TTL   = 300  # 5 min -- see module docstring for the credit-budget math
TIMEOUT     = 15
AGENT       = "hotspot-dashboard/1.0"
RADIUS_KM   = 40   # ~25 miles -- close enough to read as genuinely
                    # "overhead" local traffic, wide enough to still
                    # catch airliners cruising past at altitude. Not
                    # user-configurable in v1, same as wspr_activity.py's
                    # fixed RADIUS_METERS.

# Verbatim from OpenSky's own opensky_api.py StateVector docstring.
# 0/1 mean "no category info at all" -- left as None rather than a
# fabricated label.
_CATEGORY_LABELS = {
    2: "Light aircraft", 3: "Small aircraft", 4: "Large aircraft",
    5: "Large aircraft", 6: "Heavy aircraft", 7: "High-performance",
    8: "Rotorcraft", 9: "Glider", 10: "Airship/balloon",
    11: "Skydiver", 12: "Ultralight", 14: "UAV/drone", 15: "Spacecraft",
}

M_TO_FT   = 3.28084
MS_TO_KT  = 1.94384
MS_TO_FPM = 196.850


def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """lamin, lomin, lamax, lomax around a point -- plain equirectangular
    approximation (1 deg lat ~= 111km, 1 deg lon scaled by cos(lat)),
    fine at this radius/latitude scale."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class FlightsClient:
    """Stateless per-request client, same shape as WsprActivityClient/
    SatelliteTracker -- no credentials, so no _rebuild_*_client() needed
    in app.py, just a module-load-time singleton."""

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
        lamin, lomin, lamax, lomax = _bbox(lat, lon, RADIUS_KM)
        params = urllib.parse.urlencode({
            "lamin": round(lamin, 4), "lomin": round(lomin, 4),
            "lamax": round(lamax, 4), "lomax": round(lomax, 4),
        })
        req = urllib.request.Request(
            f"{OPENSKY_URL}?{params}", headers={"User-Agent": AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"flights.py: OpenSky fetch failed: {e}")
            return None

        aircraft = []
        for state in (data.get("states") or []):
            if len(state) < 12:
                continue
            slon, slat = state[5], state[6]
            if slon is None or slat is None or state[8]:  # state[8] = on_ground
                continue
            dist_km = _haversine_km(lat, lon, slat, slon)
            if dist_km > RADIUS_KM:
                continue

            baro_alt_m  = state[7]
            geo_alt_m   = state[13] if len(state) > 13 else None
            alt_m       = baro_alt_m if baro_alt_m is not None else geo_alt_m
            velocity_ms = state[9]
            vrate_ms    = state[11]
            category    = state[17] if len(state) > 17 else None
            callsign    = (state[1] or "").strip() or None

            aircraft.append({
                "icao24":       state[0],
                "callsign":     callsign or (state[0] or "").upper(),
                "has_callsign": callsign is not None,
                "lat": slat, "lon": slon,
                "distance_km":  round(dist_km, 1),
                "bearing_deg":  round(_bearing_deg(lat, lon, slat, slon)),
                "altitude_ft":  round(alt_m * M_TO_FT) if alt_m is not None else None,
                "speed_kt":     round(velocity_ms * MS_TO_KT) if velocity_ms is not None else None,
                "heading_deg":  round(state[10]) if state[10] is not None else None,
                "vrate_fpm":    round(vrate_ms * MS_TO_FPM) if vrate_ms is not None else None,
                "squawk":       state[14] if len(state) > 14 else None,
                "category":     _CATEGORY_LABELS.get(category),
            })
        aircraft.sort(key=lambda a: a["distance_km"])
        return {
            "aircraft":   aircraft,
            "radius_km":  RADIUS_KM,
            "fetched_at": time.time(),
        }
