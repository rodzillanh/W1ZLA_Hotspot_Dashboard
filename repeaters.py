"""Nearby repeaters via hearham.com's open worldwide directory -- confirmed
live (2026-09) as a free, no-auth, single bulk JSON GET
(https://hearham.com/api/repeaters/v1, 22,670 rows / ~9MB uncompressed in
the live sample) -- no per-user credential needed, unlike RepeaterBook's
own API, which requires an approval-gated personal token (confirmed by
reading github.com/kd9taw/Nexus's own RepeaterBook client -- `rbuapp_...`,
generated from a RepeaterBook account -- before choosing hearham instead).

Real schema, confirmed via a live full download, NOT assumed from Nexus's
own parser or a guess at plausible field names (the wspr.rx tx_call/
tx_sign trap elsewhere in this project's history is exactly the mistake
this avoids):
    id, callsign, latitude, longitude, city, group, internet_node, mode,
    encode, decode, frequency (Hz), offset (Hz), description, power,
    operational (0/1), restriction.

`mode` is genuinely messy free text, not a clean enum -- confirmed live,
not assumed: "DMR", "DMR    " (trailing whitespace baked into the source
data itself), "D-STAR", "D-star", "DSTAR", "YSF/FM", "YSF/FM ", "P25/FM",
"NXDN    ", etc. all appear as DISTINCT literal strings in the same feed.
_normalize_mode() classifies via case-insensitive substring match into a
small badge set (FM/DMR/D-STAR/FUSION/P25/NXDN/OTHER) rather than
trusting exact string equality -- order matters (more specific digital
modes are checked before the bare "fm" substring, since "DMR/FM"/
"YSF/FM"/"P25/FM" all also contain "fm").

`encode` is ALSO overloaded per mode -- confirmed live, not assumed: FM
rows carry a CTCSS tone ("100.0", "88.5", or "0"/"0.00"/blank for none);
DMR rows carry a Color Code ("CC1".."CC4"); D-Star rows carry a single
module letter ("C"). Showing "CC1" under a generic "Tone" label would be
actively wrong, not just imprecise -- _tone_label() interprets `encode`
according to the row's own normalized mode. `decode` is frequently blank
even when `encode` is set (confirmed on real DMR/D-Star rows) -- not
used here at all, since every case observed where it differed from
`encode` was one of them simply being empty.

`operational` (0/1) is a real, meaningful flag (19,208 of 22,670 rows
were 1 in the live sample, not "always 1") -- operational==0 rows are
dropped entirely by the caller (see app.py's /api/repeaters), same
"don't show something as usable that isn't" instinct as satellites.py's
own DEFAULT_SATELLITES filtering elsewhere in this project.

Distance/bearing enrichment and the radius filter live in app.py's
/api/repeaters route (reusing its existing _haversine_bearing() helper),
not here -- same division of responsibility as pota.py (this module
just fetches+normalizes; app.py's route does the geo math), so this
module never needs to know about settings.station_grid at all.
"""
import json
import time
import urllib.request

_URL = "https://hearham.com/api/repeaters/v1"
_UA = "W1ZLAHotspotDashboard/1.0 (+https://github.com/rodzillanh/W1ZLA_Hotspot_Dashboard)"
_TIMEOUT = 60
# A ~9MB worldwide directory of physical repeaters changes on the order of
# weeks, not hours -- Nexus's own choice for the identical feed is a 7-day
# TTL; this is a bit fresher but still conservative for the same reason.
_CACHE_TTL = 86400

_MODE_KEYWORDS = [
    ("dstar", "D-STAR"), ("d-star", "D-STAR"),
    ("dmr", "DMR"),
    ("ysf", "FUSION"), ("fusion", "FUSION"),
    ("p25", "P25"),
    ("nxdn", "NXDN"),
    ("fm", "FM"),  # last -- "DMR/FM"/"YSF/FM"/"P25/FM" all contain "fm" too
]


def _normalize_mode(raw) -> str:
    low = (raw or "").strip().lower()
    if not low:
        return "OTHER"
    for key, label in _MODE_KEYWORDS:
        if key in low:
            return label
    return "OTHER"


def _tone_label(mode_group: str, encode) -> "str | None":
    enc = (encode or "").strip()
    # "0"/"0.0"/"0.00" means "no tone/module set" regardless of mode --
    # confirmed live: D-STAR rows with encode="0" are NOT a real module
    # named "0" (D-Star modules are letters, A/B/C), they're blank slots
    # that happen to be encoded as "0" the same way an FM row with no
    # CTCSS is -- caught by testing against the real full dataset, not
    # assumed, after an early version showed a misleading "Mod 0" badge.
    if not enc or enc in ("0", "0.0", "0.00"):
        return None
    if mode_group == "DMR":
        return enc if enc.upper().startswith("CC") else f"CC {enc}"
    if mode_group == "D-STAR":
        # Real D-Star modules are a single letter (A/B/C/...) -- confirmed
        # against the real data ("Mod B"/"Mod C" are common). Anything
        # else (a CTCSS-looking value on a dual-mode repeater, or some
        # other code) is shown as-is rather than guessing it's a module.
        return f"Mod {enc}" if len(enc) == 1 and enc.isalpha() else enc
    # FM family (and OTHER/P25/NXDN, which don't get a more specific
    # interpretation here -- P25's NAC and NXDN's RAN codes are shown
    # as their own raw text via the ValueError branch below rather than
    # guessed at, since neither was confirmed against real documentation
    # the way DMR's Color Code / D-Star's module letter were) -- a CTCSS
    # tone, or the raw code if it doesn't parse as one.
    try:
        val = float(enc)
        return f"{val:.1f}" if val > 0 else None
    except ValueError:
        return enc


class RepeaterDirectoryClient:
    """One shared in-memory cache of the whole hearham directory, refetched
    at most once per _CACHE_TTL regardless of how many browser tabs poll
    /api/repeaters -- same "one shared server-side poll" shape as every
    other integration in this app. Never raises out of get() -- returns
    the last good cache (possibly None on a cold start) on any fetch
    failure, same degrade-gracefully contract as every other client here."""

    def __init__(self):
        self._cache: "list | None" = None
        self._cached_at = 0.0

    def get(self) -> "list | None":
        now = time.time()
        if self._cache is not None and (now - self._cached_at) < _CACHE_TTL:
            return self._cache
        try:
            req = urllib.request.Request(_URL, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 -- never raise out of a client's public methods
            print(f"[repeaters] fetch failed: {type(e).__name__}: {e}", flush=True)
            return self._cache  # stale cache (possibly None) beats a hard failure
        out = []
        for r in raw:
            mode_group = _normalize_mode(r.get("mode"))
            out.append({
                "id": r.get("id"),
                "callsign": (r.get("callsign") or "").strip(),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "city": (r.get("city") or "").strip(),
                "group": (r.get("group") or "").strip(),
                "mode": mode_group,
                "tone": _tone_label(mode_group, r.get("encode")),
                "frequency_hz": r.get("frequency"),
                "offset_hz": r.get("offset"),
                "operational": bool(r.get("operational", 1)),
            })
        self._cache = out
        self._cached_at = now
        return out
