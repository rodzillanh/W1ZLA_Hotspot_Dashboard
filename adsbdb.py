"""Aircraft type/registration + flight route lookup for the Flights
Overhead card's detail drawer -- adsbdb.com (api.adsbdb.com), a free,
keyless, MIT-licensed public API (github.com/mrjackwills/adsbdb).
Confirmed live before building against it, not assumed from docs alone:

    curl https://api.adsbdb.com/v0/aircraft/a52cc8?callsign=RPA4729

returned a real combined aircraft+route record (Embraer 175, registration
N432YX, Charleston -> Washington Reagan) in one request -- exactly the
two gaps flights.py's own docstring disclosed as unavailable from
OpenSky's live feed (aircraft type/model and route). An unknown icao24/
callsign returns a clean 404, not an error.

One real, disclosed nuance this project's own discipline requires
surfacing rather than quietly assuming away: adsbdb's flight-route data
carries an attribution note that it "may not be copied, published, or
incorporated into other databases" without the original compiler's
permission. That reads as aimed at bulk-republishing/scraping their
dataset (this module never does that -- no bulk endpoint, no persistent
storage of results beyond a short in-memory cache), not at showing a
single live lookup result in a dashboard the way this API is plainly
designed to be used (the same way other real ADS-B dashboards already
consume it) -- but it's genuinely a bit ambiguous, so this client is
deliberately conservative: fetched on-demand per aircraft (only when a
user opens that flight's detail drawer, never for the whole list on
every poll), cached briefly, never written to disk.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ADSBDB_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
CACHE_TTL = 3600  # aircraft type/registration and a callsign's usual
                   # route don't change flight to flight -- still avoid
                   # re-querying on every card poll for the same aircraft
TIMEOUT = 15
AGENT = "hotspot-dashboard/1.0"


class AdsbdbClient:
    """Stateless per-request client, same shape as every other free/
    keyless integration in this app -- no credentials, in-memory cache
    only (see module docstring for why this deliberately never persists
    results to disk)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict | None]] = {}

    def lookup(self, icao24: str, callsign: str | None) -> dict | None:
        icao24 = (icao24 or "").strip().lower()
        if not icao24:
            return None
        key = f"{icao24}|{(callsign or '').strip().upper()}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and (time.time() - cached[0]) < CACHE_TTL:
                return cached[1]

        result = self._fetch(icao24, callsign)
        with self._lock:
            self._cache[key] = (time.time(), result)
        return result

    @staticmethod
    def _fetch(icao24: str, callsign: str | None) -> dict | None:
        url = ADSBDB_URL.format(icao24=urllib.parse.quote(icao24))
        if callsign:
            url += "?" + urllib.parse.urlencode({"callsign": callsign.strip().upper()})
        req = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}  # legit "nothing known about this aircraft/callsign" --
                           # distinct from a fetch failure (None), same
                           # "don't fabricate, don't retry a real negative"
                           # pattern as every other client here
            print(f"adsbdb.py: fetch failed: {e}")
            return None
        except Exception as e:
            print(f"adsbdb.py: fetch failed: {e}")
            return None

        resp_data = (data or {}).get("response")
        if not isinstance(resp_data, dict):
            return {}

        result = {}
        aircraft = resp_data.get("aircraft") or {}
        if aircraft.get("type"):
            result["aircraft_type"] = aircraft.get("type")
            result["registration"] = aircraft.get("registration")
            result["manufacturer"] = aircraft.get("manufacturer")
            result["owner"] = aircraft.get("registered_owner")

        route = resp_data.get("flightroute") or {}
        origin, dest = route.get("origin") or {}, route.get("destination") or {}
        if origin.get("icao_code") and dest.get("icao_code"):
            result["route"] = {
                "airline": (route.get("airline") or {}).get("name"),
                "origin": {
                    "code": origin.get("iata_code") or origin.get("icao_code"),
                    "name": origin.get("name"),
                    "municipality": origin.get("municipality"),
                },
                "destination": {
                    "code": dest.get("iata_code") or dest.get("icao_code"),
                    "name": dest.get("name"),
                    "municipality": dest.get("municipality"),
                },
            }
        return result
