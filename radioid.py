"""Minimal client for RadioID.net's public DMR user database.

Docs: https://radioid.net/api/
No API key or login required. One HTTP call per lookup:
  https://radioid.net/api/dmr/user/?callsign=<callsign>

Unlike QRZ, RadioID.net has no lat/lon data -- it only returns name and
city/state/country text, sourced from each operator's DMR ID registration.
This makes it a good *free* fallback for caller name/location when QRZ
isn't subscribed (or doesn't have the callsign), but it can't contribute
map coordinates.
"""
import time
import threading
import urllib.parse
import urllib.request
import json
from typing import Optional

import config

RADIOID_BASE_URL = "https://radioid.net/api/dmr/user/"


class RadioIdClient:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cache: dict = {}  # callsign -> (expiry_epoch, result|None)

    def lookup(self, callsign: str) -> Optional[dict]:
        """Return dict with name/location, or None if not found / on error."""
        with self._lock:
            cached = self._cache.get(callsign)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._lookup_remote(callsign)
        with self._lock:
            self._cache[callsign] = (time.time() + config.RADIOID_CACHE_TTL, result)
        return result

    def _lookup_remote(self, callsign: str) -> Optional[dict]:
        try:
            params = urllib.parse.urlencode({"callsign": callsign})
            req = urllib.request.Request(
                f"{RADIOID_BASE_URL}?{params}",
                headers={"User-Agent": config.RADIOID_AGENT},
            )
            with urllib.request.urlopen(req, timeout=config.RADIOID_TIMEOUT) as resp:
                data = json.loads(resp.read())
            results = data.get("results") or []
            if not results:
                return None
            row = results[0]
            fname = (row.get("fname") or "").strip()
            lname = (row.get("name")  or "").strip()
            name  = " ".join(p for p in (fname, lname) if p) or None
            city    = row.get("city")
            state   = row.get("state")
            country = row.get("country")
            location = ", ".join(p for p in (city, state or country) if p) or None
            return {"name": name, "location": location}
        except Exception:
            return None
