"""Best-effort "Starlink train" visual-sighting detection for the
Satellites card -- a real, disclosed, more fragile feature than every
other integration in this app, kept deliberately separate/opt-in and
labeled as such in the UI rather than presented with the same
confidence as satellites.py's own regular pass predictions.

Two real gaps in this data source, confirmed live before building
against it, not assumed:

1. There is no documented JSON/API index of "which Starlink launch
   batches are currently listed" on CelesTrak's supplemental-elements
   page (celestrak.org/NORAD/elements/supplemental/) -- only an HTML
   page that happens to always show the single most recent
   "Post-Deployment" batch (confirmed live: at time of writing this
   was "Starlink G10-19 Post-Deployment", deployed hours earlier the
   same day). Getting the current batch's file slug means regex-
   matching that HTML for a "Starlink G<N>-<N> Post-Deployment"
   string immediately followed by its own `FILE=<slug>` link -- a real
   scrape of page structure, not a stable API contract, unlike every
   other data source in this project. If CelesTrak ever changes this
   page's wording/markup, `_GROUP_FILE_RE` silently stops matching and
   this returns None (same degrade-gracefully contract as every other
   client here) rather than erroring or fabricating a batch.
2. In the hours right after a launch, a batch may only have 1-2
   tracked objects -- confirmed live against the real, same-day
   G10-19 launch: exactly a "STACK" (the still-combined upper stage +
   satellites) and one rough "SINGLE" object, with near-identical
   orbital elements (same RAAN, inclination to 0.0001 deg, mean
   anomaly 0.25 deg apart -- genuinely still a tight cluster), well
   before the full ~20-28 individual satellites a real launch carries
   get their own catalogued TLEs. So a "train" shown here can under-
   represent how many satellites are actually visible in the sky --
   this module reports exactly what CelesTrak currently has tracked,
   it never pads the count out with invented satellites.

Fetches the batch as plain TLE lines (`FORMAT=tle`), not the richer
OMM JSON also available at the same endpoint -- confirmed live that
`sup-gp.php` serves real TLE-format text for a supplemental batch, so
this reuses the exact same `Satrec.twoline2rv()` parsing satellites.py
already uses for its own per-satellite TLE fetch, rather than adding a
second orbital-element parser (OMM JSON has a different field-by-field
shape) for what both feed into an identical SGP4 propagation.

Pass-finding and the sunlit/visible check are NOT reimplemented here --
`SatelliteTracker._find_passes` (satellites.py) is called directly per
satellite, the same machinery serving the regular ham-satellite pass
list. Results are filtered to `visible=True` only: a "train" sighting
is fundamentally a visual phenomenon, so a pass nobody could actually
see isn't worth surfacing here the way it is for a ham satellite you'd
work by RF regardless of visibility.
"""
import re
import threading
import time
import urllib.request

from sgp4.api import Satrec

import satellites as sat_mod

SUPPLEMENTAL_INDEX_URL = "https://celestrak.org/NORAD/elements/supplemental/"
SUPPLEMENTAL_TLE_URL_TMPL = (
    "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php?FILE={slug}&FORMAT=tle"
)
# Matches e.g. `Starlink G10-19 Post-Deployment</a> <a ... href="sup-gp.php
# ?FILE=starlink-g10-19&FORMAT=csv">` -- captures the group label for
# display and the FILE= slug for the TLE fetch, anchored so the slug has
# to immediately follow THIS specific "Post-Deployment" link (the same
# page also lists an unrelated upcoming pre-launch group + its backup
# opportunities right after this one, which must NOT match).
_GROUP_FILE_RE = re.compile(
    r'Starlink (G\d+-\d+) Post-Deployment</a>\s*<a[^>]*href="sup-gp\.php\?FILE=([a-z0-9-]+)&FORMAT=csv"'
)

BATCH_CACHE_TTL = 6 * 3600  # which launch is "current" doesn't change more
                             # than a few times a day at most
PASS_CACHE_TTL  = 300
TIMEOUT = 15
AGENT   = "hotspot-dashboard/1.0"
MAX_SATS_PER_BATCH = 40     # a full Starlink launch carries ~20-28 -- this
                             # is a sanity cap, not a real observed count
MAX_TRAIN_PASSES = 8


class StarlinkTrainClient:
    """Best-effort -- see module docstring for the two real gaps this
    has that no other integration in this app does (HTML scrape for
    the current batch, and a possibly-incomplete satellite count right
    after launch)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._batch_cache = None  # (fetch_time, group_label, [(Satrec, name), ...])
        self._pass_cache: dict[tuple, tuple[float, dict]] = {}

    def _get_batch(self):
        with self._lock:
            if self._batch_cache and (time.time() - self._batch_cache[0]) < BATCH_CACHE_TTL:
                return self._batch_cache[1], self._batch_cache[2]

        fetched = self._fetch_batch()
        if fetched is None:
            with self._lock:
                if self._batch_cache:
                    return self._batch_cache[1], self._batch_cache[2]
            return None, None

        group_label, sats = fetched
        with self._lock:
            self._batch_cache = (time.time(), group_label, sats)
        return group_label, sats

    @staticmethod
    def _fetch_batch():
        try:
            req = urllib.request.Request(SUPPLEMENTAL_INDEX_URL, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"starlink_trains.py: index fetch failed: {e}")
            return None

        m = _GROUP_FILE_RE.search(html)
        if not m:
            return None  # no post-deployment group currently listed -- not an error
        group_label, slug = m.groups()

        try:
            req = urllib.request.Request(
                SUPPLEMENTAL_TLE_URL_TMPL.format(slug=slug), headers={"User-Agent": AGENT}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"starlink_trains.py: batch TLE fetch failed: {e}")
            return None

        lines = [ln for ln in text.splitlines() if ln.strip()]
        sats = []
        for i in range(0, len(lines) - 2, 3):
            name, line1, line2 = lines[i].strip(), lines[i + 1], lines[i + 2]
            if not (line1.startswith("1 ") and line2.startswith("2 ")):
                continue
            try:
                satrec = Satrec.twoline2rv(line1, line2)
            except Exception:
                continue
            sats.append((satrec, name))
            if len(sats) >= MAX_SATS_PER_BATCH:
                break
        if not sats:
            return None
        return group_label, sats

    def visible_passes(self, observer_lat: float, observer_lon: float) -> dict | None:
        """{"group": "G10-19", "satellite_count": N, "passes": [...]}
        or None if there's currently no post-deployment batch listed, or
        the fetch failed with nothing cached yet. Passes are the SAME
        shape satellites.py's regular pass list uses (aos_epoch/
        los_epoch/max_elevation/aos_azimuth/los_azimuth/visible),
        pre-filtered to visible=True only."""
        group_label, sats = self._get_batch()
        if not sats:
            return None

        key = (group_label, round(observer_lat, 2), round(observer_lon, 2))
        with self._lock:
            cached = self._pass_cache.get(key)
            if cached and (time.time() - cached[0]) < PASS_CACHE_TTL:
                return cached[1]

        observer_ecef = sat_mod._geodetic_to_ecef(observer_lat, observer_lon, 0.0)
        all_passes = []
        for satrec, name in sats:
            sat_cfg = {"norad_id": satrec.satnum}
            found = sat_mod.SatelliteTracker._find_passes(
                satrec, sat_cfg, name, observer_ecef, observer_lat, observer_lon
            )
            all_passes.extend(p for p in found if p["visible"])
        all_passes.sort(key=lambda p: p["aos_epoch"])

        result = {
            "group": group_label,
            "satellite_count": len(sats),
            "passes": all_passes[:MAX_TRAIN_PASSES],
        }
        with self._lock:
            self._pass_cache[key] = (time.time(), result)
        return result
