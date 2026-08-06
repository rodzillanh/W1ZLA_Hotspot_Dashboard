"""Minimal client for the aprs.fi location API.

Docs: https://aprs.fi/page/api
Requires a free API key (sign up at aprs.fi). One HTTP call per lookup:
  https://api.aprs.fi/api/get?name=<callsign>&what=loc&apikey=<key>&format=json

Returns the caller's most recent APRS beacon position, which is often more
useful than QRZ's static "home station" coordinates for mobile/portable
operators. Results are cached briefly since APRS positions can move.
"""
import time
import threading
import urllib.parse
import urllib.request
import json
from typing import Optional

import config

APRS_BASE_URL = "https://api.aprs.fi/api/get"


class AprsClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self._lock  = threading.Lock()
        self._cache: dict = {}  # callsign -> (expiry_epoch, result|None)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def lookup(self, callsign: str) -> Optional[dict]:
        """Return dict with lat/lon/comment/time, or None if unavailable."""
        if not self.enabled:
            return None
        with self._lock:
            cached = self._cache.get(callsign)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._lookup_remote(callsign)
        with self._lock:
            self._cache[callsign] = (time.time() + config.APRS_CACHE_TTL, result)
        return result

    def _lookup_remote(self, callsign: str) -> Optional[dict]:
        try:
            params = urllib.parse.urlencode({
                "name":   callsign,
                "what":   "loc",
                "apikey": self.api_key,
                "format": "json",
            })
            with urllib.request.urlopen(f"{APRS_BASE_URL}?{params}", timeout=config.APRS_TIMEOUT) as resp:
                data = json.loads(resp.read())
            if data.get("result") != "ok" or not data.get("entries"):
                return None
            entry = data["entries"][0]
            return {
                "lat":     float(entry["lat"]),
                "lon":     float(entry["lng"]),
                "comment": entry.get("comment"),
                "time":    int(entry["time"]) if entry.get("time") else None,
                # Combined symbol-table-char + symbol-code-char string, e.g.
                # "/>" for a car. Confirmed live (2026-08-06) against a real
                # what=loc response for W1ZLA's own DigiPi APRS beacon --
                # returned exactly {"symbol":"/>","comment":"DigiPi WebChat
                # Beacon",...}, matching the third-party docs this was
                # originally sourced from field-for-field. Table char is
                # entry[0] ('/' = primary, '\\' = alternate per the APRS
                # spec), code char is entry[1].
                "symbol":  entry.get("symbol"),
            }
        except Exception:
            return None

    def test_key(self) -> tuple[bool, str]:
        """Check the API key itself (distinct from lookup(), which returns
        None for both a bad key and a valid key with no matching station)."""
        try:
            params = urllib.parse.urlencode({
                "name": "APRS", "what": "loc", "apikey": self.api_key, "format": "json",
            })
            with urllib.request.urlopen(f"{APRS_BASE_URL}?{params}", timeout=config.APRS_TIMEOUT) as resp:
                data = json.loads(resp.read())
            if data.get("result") == "ok":
                return True, "Connected — API key accepted"
            return False, data.get("description", "API key rejected")
        except Exception as e:
            return False, str(e)
