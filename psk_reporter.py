"""Live PSK Reporter reception reports for the Live map's optional "PSK
Reporter" overlay -- shows where YOUR OWN signal was actually heard,
which is a genuinely different question than "who else is active" (the
WSPR spots overlay this app removed for being visual clutter). Scoped to
one callsign by design (PSK Reporter's own query API is fundamentally
"reports for this sender"), so there's no equivalent clutter risk here.

Endpoint/params/response format confirmed by reading
https://pskreporter.info/pskdev.html directly (curl, not a summarizer --
WebFetch got a bare 403 against that URL) and by a real, live test query
before writing this:
  https://retrieve.pskreporter.info/query?senderCallsign=<call>&flowStartSeconds=-<secs>&rronly=1
Response is XML (NOT JSON, despite some secondhand summaries claiming
otherwise), one <receptionReport> element per report with the sender's
data as plain XML ATTRIBUTES (receiverCallsign, receiverLocator,
senderCallsign, frequency, flowStartSeconds), not child tags -- a
different shape than qrz.py's child-tag XML, confirmed against the
docs page's own sample document, not assumed:
    <receptionReport receiverCallsign="KB1MBX" receiverLocator="FN42hn"
                      senderCallsign="N1DQ" frequency="14070987"
                      flowStartSeconds="1784767174"/>
`frequency` is already Hz. `flowStartSeconds` in a response is an
absolute Unix epoch (confirmed against the response's own
`currentSeconds` attribute, which matched "now" at request time), NOT
the negative relative offset the REQUEST parameter of the same name
uses -- same name, different meaning on each side of the call, a real
gotcha worth remembering if this is ever touched again.

**Rate limiting is real and aggressive, confirmed the hard way**: a
handful of test queries a couple minutes apart from the same IP got a
`{"message": "Your IP has made too many queries too often..."}` JSON
error back (yes, the error itself is JSON even though success responses
are XML). The docs ask for "no more than once every five minutes" per
IP; CACHE_TTL here is set well above that with real margin, and this
must stay a single shared client instance -- never add a second caller
of _fetch() without accounting for the fact that EVERY dashboard running
this code shares one global rate budget from PSK Reporter's perspective
per source IP, same caution as wspr.live but enforced far more strictly.

`mode` is documented only as a request FILTER parameter, not confirmed
as a default output attribute on a live response (rate-limited before
that could be independently re-verified) -- not relied on here for that
reason; re-verify with a real live query before ever trying to surface
mode/read it from a response.
"""
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from wspr_activity import grid_to_latlon
from wsjtx import freq_to_band

PSK_REPORTER_URL = "https://retrieve.pskreporter.info/query"
CACHE_TTL = 600  # 10 min -- comfortably above PSK Reporter's own stated
                 # "no more than once every 5 minutes" guidance, with
                 # real margin after being rate-limited during testing.
SPOT_WINDOW_SEC = 1800  # 30 min -- how far back a reception report can
                        # be and still show on the map; self-ages out.
TIMEOUT = 15
AGENT = "hotspot-dashboard/1.0"


class PskReporterClient:
    """Cached per-callsign -- there's only ever one "my callsign" per
    dashboard instance (settings.psk_reporter_callsign), so a single
    cache slot (not a dict keyed by callsign) is all this needs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_call = None
        self._cache_time = 0.0

    def get(self, callsign: str, force: bool = False) -> dict | None:
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return None
        with self._lock:
            fresh = (
                not force and self._cache is not None
                and self._cache_call == callsign
                and (time.time() - self._cache_time) < CACHE_TTL
            )
            if fresh:
                return self._cache

        result = self._fetch(callsign)
        with self._lock:
            if result is not None:
                self._cache      = result
                self._cache_call = callsign
                self._cache_time = time.time()
                return self._cache
            # Degrade gracefully -- serve the last good snapshot for the
            # SAME callsign on a transient failure, same pattern as
            # wspr_activity.py's WsprActivityClient.
            if self._cache_call == callsign:
                return self._cache
            return None

    @staticmethod
    def _fetch(callsign: str) -> dict | None:
        params = {
            "senderCallsign": callsign,
            "flowStartSeconds": str(-SPOT_WINDOW_SEC),
            "rronly": "1",
        }
        url = PSK_REPORTER_URL + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
        except Exception:
            return None

        spots = []
        for elem in root.findall("receptionReport"):
            grid = elem.get("receiverLocator")
            latlon = grid_to_latlon(grid) if grid else None
            if latlon is None:
                continue  # can't plot without a position
            lat, lon = latlon
            try:
                freq_hz = int(elem.get("frequency"))
            except (TypeError, ValueError):
                freq_hz = None
            try:
                heard_at = int(elem.get("flowStartSeconds"))
            except (TypeError, ValueError):
                heard_at = None
            spots.append({
                "receiver_call": elem.get("receiverCallsign"),
                "band": freq_to_band(freq_hz) if freq_hz else "",
                "lat": lat, "lon": lon,
                "heard_at": heard_at,
            })
        return {"spots": spots, "fetched_at": time.time()}
