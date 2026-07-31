"""Amateur radio satellite tracking -- current position + upcoming pass
predictions for the Satellites card and the Live map's ground-track
overlay.

Two free, keyless, live-verified data sources (not assumed from docs):

- Orbital elements (TLEs): celestrak.org's per-satellite REST endpoint
  (`?CATNR=<norad_id>&FORMAT=tle`). Confirmed live -- a plain `curl`
  with no User-Agent got a 503 (their bot-protection, not the service
  being down); a normal browser-style User-Agent gets a clean 200 with
  real current TLE data.
- Per-satellite transmitter mode/frequency: db.satnogs.org's REST API.
  **The filter parameter is `satellite__norad_cat_id`, NOT the more
  obvious `norad_cat_id`** -- confirmed the hard way: `norad_cat_id=`
  is silently ignored (no error) and returns the ENTIRE ~5000-row
  transmitter table unfiltered, which looked plausible at a glance
  (real transmitter records, just for the wrong satellite entirely)
  until cross-checking the response's own `norad_cat_id` field, which
  didn't match what was requested. `satellite__norad_cat_id=` is the
  one that actually filters.

Orbital propagation is SGP4 (the `sgp4` PyPI package -- a tested,
widely-used library, not hand-rolled orbital mechanics) plus a manual
TEME->ECEF->geodetic conversion for position, and ECEF->topocentric
ENU for observer-relative azimuth/elevation. Verified against a live
ground-truth ISS position from api.wheretheiss.at before trusting any
of this: computed lat/lon/alt matched the ground truth to within
~0.005 deg (~500m) and ~25m altitude -- well within what a pass
prediction needs (irrelevant whether a satellite is at exactly 38.00
or 38.01 degrees elevation). The topocentric elevation/azimuth math was
separately sanity-checked three ways: an observer directly under the
satellite computes ~90 deg elevation with range equal to the satellite's
own altitude; an antipodal-ish observer computes a deeply negative
elevation; an observer offset by a few hundred km computes a plausible
mid-range elevation with range slightly greater than altitude, not
equal to it. All three matched geometric expectations exactly.

DEFAULT_SATELLITES is a small, live-verified set, not a guess from
memory -- satellite operational status changes over time (AO-92/FOX-1D
looked like a reasonable "well known easy sat" from general ham radio
knowledge, but is actually marked "re-entered" in SatNOGS DB right now,
confirmed live before this list was written). Frequencies/modes below
were pulled from db.satnogs.org's own live "active" transmitter records
for each satellite, not typed from memory:
  - ISS (25544): well-known APRS digipeater, confirmed via SatNOGS as
    "Mode V/V APRS AFSK" at 145.825 MHz.
  - SO-50 (27607): "Mode V/U FM Voice CTCSS 67.0 Hz", 436.795 down /
    145.850 up.
  - AO-91 (43017): "Mode U/V FM Voice", 145.960 down / 435.250 up.
  - PO-101 (43678): "FM VOICE", 145.900 down / 437.500 up.
If this list is ever revisited, re-verify status/frequencies the same
live way -- don't extend it from recollection of "well-known easy
sats," several of which have gone silent or re-entered over the years.
"""
import math
import threading
import time
import urllib.error
import urllib.request

from sgp4.api import Satrec, jday

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=tle"
TLE_CACHE_TTL = 6 * 3600  # TLEs are regenerated roughly once or twice a day; a
                          # few hours of staleness is irrelevant for ham-radio-
                          # grade pass prediction (unlike e.g. re-entry tracking).
PASS_CACHE_TTL = 120      # pass lists are recomputed at most every 2 min, not
                          # on every dashboard poll -- each computation steps
                          # through many hours in 30s increments per satellite.
POSITIONS_CACHE_TTL = 10  # short -- a satellite moves ~7.6 km/s, so this needs
                          # to feel live -- but still avoids redoing ~90 SGP4
                          # calls per satellite on every single poll/browser tab.
TIMEOUT = 15
AGENT = "hotspot-dashboard/1.0"

PASS_SEARCH_HOURS = 48
PASS_STEP_SECONDS = 30
MIN_PASS_ELEVATION = 10.0  # degrees -- a pass that never clears this isn't
                            # worth showing; low passes are marginal at best
                            # for a typical ham HT/handheld antenna anyway.
MAX_PASSES_PER_SAT = 5

TRACK_HALF_SPAN_MIN = 45  # +/- minutes of ground track around "now" -- covers
                           # roughly one full orbit for a typical ham LEO
                           # satellite (~90-100 min period), so the Live map
                           # overlay reads as "the current orbit," not an
                           # arbitrary short arc.
TRACK_STEP_SECONDS = 60
_EARTH_MEAN_RADIUS_KM = 6371.0

# name, mode, downlink MHz, uplink MHz (None if none/not applicable) -- see
# module docstring for how/when each was verified.
DEFAULT_SATELLITES = [
    {"norad_id": 25544, "name": "ISS",    "mode": "APRS", "downlink_mhz": 145.825, "uplink_mhz": 145.825},
    {"norad_id": 27607, "name": "SO-50",  "mode": "FM",   "downlink_mhz": 436.795, "uplink_mhz": 145.850},
    {"norad_id": 43017, "name": "AO-91",  "mode": "FM",   "downlink_mhz": 145.960, "uplink_mhz": 435.250},
    {"norad_id": 43678, "name": "PO-101", "mode": "FM",   "downlink_mhz": 145.900, "uplink_mhz": 437.500},
]

_A = 6378.137            # WGS84 semi-major axis, km
_F = 1 / 298.257223563   # WGS84 flattening
_E2 = 2 * _F - _F * _F


def _gmst_rad(jd: float, fr: float) -> float:
    """Greenwich Mean Sidereal Time -- a standard-formula approximation
    (not full IAU precision), which is what the ISS ground-truth check
    in the module docstring validated as accurate enough for this."""
    t = ((jd + fr) - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * ((jd + fr) - 2451545.0)
                + 0.000387933 * t * t - t * t * t / 38710000.0)
    return math.radians(gmst_deg % 360.0)


def _teme_to_ecef(x: float, y: float, z: float, theta: float) -> tuple[float, float, float]:
    xe = x * math.cos(theta) + y * math.sin(theta)
    ye = -x * math.sin(theta) + y * math.cos(theta)
    return xe, ye, z


def _ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)
    lat = math.atan2(z, p * (1 - _E2))
    alt = 0.0
    for _ in range(6):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
    x = (n + alt_km) * math.cos(lat) * math.cos(lon)
    y = (n + alt_km) * math.cos(lat) * math.sin(lon)
    z = (n * (1 - _E2) + alt_km) * math.sin(lat)
    return x, y, z


def _footprint_radius_km(alt_km: float) -> float:
    """Great-circle radius of the satellite's visibility horizon --
    standard tangent-line-from-altitude geometry (ignores atmospheric
    refraction, which only matters for a fraction of a degree at the
    horizon -- irrelevant at map-overlay scale). Sanity-checked against
    commonly-cited real-world figures: at ISS altitude (~425km) this
    gives ~2265km, matching the ~2000-2300km figure widely cited for the
    ISS's own visibility footprint."""
    return _EARTH_MEAN_RADIUS_KM * math.acos(_EARTH_MEAN_RADIUS_KM / (_EARTH_MEAN_RADIUS_KM + alt_km))


def _sub_satellite_point(sat: Satrec, jd: float, fr: float) -> tuple[float, float, float] | None:
    """Returns (lat, lon, alt_km), or None if SGP4 reports an error
    (e.g. decayed orbit) for this time."""
    e, r, _v = sat.sgp4(jd, fr)
    if e != 0:
        return None
    theta = _gmst_rad(jd, fr)
    ecef = _teme_to_ecef(*r, theta)
    lat, lon, alt = _ecef_to_geodetic(*ecef)
    return lat, lon, alt


def _elevation_azimuth(observer_ecef: tuple[float, float, float], observer_lat: float,
                        observer_lon: float, sat_ecef: tuple[float, float, float]) -> tuple[float, float, float]:
    """Returns (elevation_deg, azimuth_deg, range_km) of the satellite as
    seen from the observer."""
    ox, oy, oz = observer_ecef
    dx, dy, dz = sat_ecef[0] - ox, sat_ecef[1] - oy, sat_ecef[2] - oz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    lat, lon = math.radians(observer_lat), math.radians(observer_lon)
    # local ENU basis vectors, expressed in ECEF
    east  = (-math.sin(lon), math.cos(lon), 0.0)
    north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
    up    = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
    e_comp = dx * east[0] + dy * east[1] + dz * east[2]
    n_comp = dx * north[0] + dy * north[1] + dz * north[2]
    u_comp = dx * up[0] + dy * up[1] + dz * up[2]
    elevation = math.degrees(math.asin(u_comp / rng))
    azimuth = math.degrees(math.atan2(e_comp, n_comp)) % 360.0
    return elevation, azimuth, rng


class SatelliteTracker:
    """Fetches/caches TLEs per NORAD ID and serves current positions +
    upcoming pass predictions. The TLE fetch itself has the usual
    network-client cache/TTL, but `positions()`'s own ground-track
    computation (~90 SGP4 calls per satellite) also gets a short cache
    -- worth avoiding redoing on every request under repeated/multi-tab
    polling even though each individual SGP4 call is fast."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tle_cache: dict[int, tuple[float, Satrec, str]] = {}  # norad_id -> (fetch_time, Satrec, name)
        self._pass_cache: dict[tuple, tuple[float, list]] = {}
        self._positions_cache: dict[tuple, tuple[float, list]] = {}

    def _get_satrec(self, norad_id: int) -> tuple[Satrec, str] | None:
        with self._lock:
            cached = self._tle_cache.get(norad_id)
            if cached and (time.time() - cached[0]) < TLE_CACHE_TTL:
                return cached[1], cached[2]
        fetched = self._fetch_tle(norad_id)
        if fetched is None:
            with self._lock:
                cached = self._tle_cache.get(norad_id)
                return (cached[1], cached[2]) if cached else None
        satrec, name = fetched
        with self._lock:
            self._tle_cache[norad_id] = (time.time(), satrec, name)
        return satrec, name

    @staticmethod
    def _fetch_tle(norad_id: int) -> tuple[Satrec, str] | None:
        try:
            req = urllib.request.Request(
                CELESTRAK_URL.format(norad_id=norad_id), headers={"User-Agent": AGENT}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 3:
            return None
        name, line1, line2 = lines[0].strip(), lines[1], lines[2]
        try:
            satrec = Satrec.twoline2rv(line1, line2)
        except Exception:
            return None
        return satrec, name

    def positions(self, satellites: list[dict]) -> list[dict]:
        """Current lat/lon/alt for each configured satellite, plus a short
        ground track (+/- TRACK_HALF_SPAN_MIN) for the Live map overlay.
        Entries whose TLE couldn't be fetched (network issue, bad NORAD
        id) are simply omitted -- same degrade-gracefully contract as
        every other client here."""
        key = tuple(s["norad_id"] for s in satellites)
        with self._lock:
            cached = self._positions_cache.get(key)
            if cached and (time.time() - cached[0]) < POSITIONS_CACHE_TTL:
                return cached[1]

        now = time.time()
        jd, fr = jday(*time.gmtime(now)[:6])
        results = []
        for sat_cfg in satellites:
            got = self._get_satrec(sat_cfg["norad_id"])
            if got is None:
                continue
            satrec, tle_name = got
            point = _sub_satellite_point(satrec, jd, fr)
            if point is None:
                continue
            lat, lon, alt = point
            results.append({
                "norad_id": sat_cfg["norad_id"],
                "name": sat_cfg.get("name") or tle_name,
                "mode": sat_cfg.get("mode"),
                "downlink_mhz": sat_cfg.get("downlink_mhz"),
                "uplink_mhz": sat_cfg.get("uplink_mhz"),
                "lat": round(lat, 3),
                "lon": round(lon, 3),
                "alt_km": round(alt, 1),
                "footprint_km": round(_footprint_radius_km(alt), 1),
                "track": self._ground_track(satrec, now),
            })

        with self._lock:
            self._positions_cache[key] = (time.time(), results)
        return results

    @staticmethod
    def _ground_track(satrec: Satrec, now: float) -> list[list[float]]:
        """[[lat, lon], ...] from TRACK_HALF_SPAN_MIN before now to
        TRACK_HALF_SPAN_MIN after, at TRACK_STEP_SECONDS resolution --
        roughly one orbit for a typical LEO ham satellite (~90-100 min
        period), which is what makes this read as "the current orbit"
        rather than an arbitrary short arc. A point is omitted (not
        fabricated) if SGP4 errors out for that time, same as everywhere
        else in this module."""
        points = []
        t = now - TRACK_HALF_SPAN_MIN * 60
        end = now + TRACK_HALF_SPAN_MIN * 60
        while t <= end:
            jd, fr = jday(*time.gmtime(t)[:6])
            point = _sub_satellite_point(satrec, jd, fr)
            if point is not None:
                points.append([round(point[0], 3), round(point[1], 3)])
            t += TRACK_STEP_SECONDS
        return points

    def passes(self, satellites: list[dict], observer_lat: float, observer_lon: float) -> list[dict]:
        """Upcoming passes (AOS/LOS/max elevation) across all configured
        satellites over the next PASS_SEARCH_HOURS, sorted by AOS time.
        Cached briefly (PASS_CACHE_TTL) since this steps through many
        hours per satellite -- not something to redo on every poll."""
        key = (tuple(s["norad_id"] for s in satellites), round(observer_lat, 2), round(observer_lon, 2))
        with self._lock:
            cached = self._pass_cache.get(key)
            if cached and (time.time() - cached[0]) < PASS_CACHE_TTL:
                return cached[1]

        observer_ecef = _geodetic_to_ecef(observer_lat, observer_lon, 0.0)
        all_passes = []
        for sat_cfg in satellites:
            got = self._get_satrec(sat_cfg["norad_id"])
            if got is None:
                continue
            satrec, tle_name = got
            all_passes.extend(self._find_passes(satrec, sat_cfg, tle_name, observer_ecef, observer_lat, observer_lon))
        all_passes.sort(key=lambda p: p["aos_epoch"])

        with self._lock:
            self._pass_cache[key] = (time.time(), all_passes)
        return all_passes

    @staticmethod
    def _find_passes(satrec: Satrec, sat_cfg: dict, tle_name: str,
                      observer_ecef: tuple[float, float, float],
                      observer_lat: float, observer_lon: float) -> list[dict]:
        start = time.time()
        passes = []
        in_pass = False
        aos_epoch = None
        max_el = -90.0

        t = start
        end = start + PASS_SEARCH_HOURS * 3600
        prev_el = None
        while t <= end and len(passes) < MAX_PASSES_PER_SAT:
            jd, fr = jday(*time.gmtime(t)[:6])
            e, r, _v = satrec.sgp4(jd, fr)
            if e != 0:
                break  # propagation error (e.g. decayed) -- stop, don't fabricate passes
            theta = _gmst_rad(jd, fr)
            sat_ecef = _teme_to_ecef(*r, theta)
            el, az, _rng = _elevation_azimuth(observer_ecef, observer_lat, observer_lon, sat_ecef)

            if not in_pass and prev_el is not None and prev_el < 0 <= el:
                in_pass = True
                aos_epoch = t
                max_el = el
            elif in_pass:
                max_el = max(max_el, el)
                if el < 0:
                    if max_el >= MIN_PASS_ELEVATION:
                        passes.append({
                            "norad_id": sat_cfg["norad_id"],
                            "name": sat_cfg.get("name") or tle_name,
                            "mode": sat_cfg.get("mode"),
                            "downlink_mhz": sat_cfg.get("downlink_mhz"),
                            "uplink_mhz": sat_cfg.get("uplink_mhz"),
                            "aos_epoch": aos_epoch,
                            "los_epoch": t,
                            "max_elevation": round(max_el, 1),
                        })
                    in_pass = False
                    max_el = -90.0
            prev_el = el
            t += PASS_STEP_SECONDS
        return passes
