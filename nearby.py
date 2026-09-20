"""Nearby lookups for Pocket Dash's "Nearby" screen (a phone's position, not the
station's): Brandmeister DMR repeaters and their static talkgroups, and AllStarLink
node numbers for repeaters the repeater directory flags as AllStar.

Sources, all free/no-auth and confirmed live (2026-09):
  * Brandmeister  https://api.brandmeister.network/v2/device -- ~33,000 devices in
    one 9.7 MB JSON list, ~29,000 with coordinates. Hotspots and repeaters are in
    the same list; `pep` (effective power) and a tx/rx split tell them apart.
    /v2/device/<id>/talkgroup -> that device's STATIC talkgroups (often empty:
    many repeaters are configured on the repeater itself). /v2/talkgroup -> names.
  * AllStarLink   https://allmondb.allstarlink.org/ -- a 1.4 MB text file,
    `node|callsign|freq|location`, ~39,000 lines. Locations are free text
    ("Whitefield, NH"), NOT coordinates, so it can't be searched by position
    directly; instead an AllStar node number is attached to a repeater from the
    repeater directory (repeaters.py, which has coordinates and a `group` of
    "Allstar") by matching callsign + frequency.

Like every client here, nothing raises out of a public method: a failed fetch
returns None (or keeps the last good copy) so the phone just shows "unavailable".
"""
import re
import threading
import time
import urllib.request
import json

BM_DEVICES_URL = "https://api.brandmeister.network/v2/device"
BM_TG_URL = "https://api.brandmeister.network/v2/device/{}/talkgroup"
BM_TG_NAMES_URL = "https://api.brandmeister.network/v2/talkgroup"
ASL_DB_URL = "https://allmondb.allstarlink.org/"

DEVICES_TTL = 6 * 3600        # a 9.7 MB download: don't repeat it often
TG_TTL = 30 * 60
TG_NAMES_TTL = 24 * 3600
ASL_TTL = 12 * 3600
STALE_DAYS = 45               # a device not heard from in this long isn't "nearby" in any useful sense
UA = "Mozilla/5.0 (W1ZLA Hotspot Dashboard)"


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _mhz(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_repeater(dev) -> bool:
    """A Brandmeister DEVICE that looks like a repeater rather than a hotspot:
    real power (hotspots register pep 1) or a duplex split."""
    tx, rx = _mhz(dev.get("tx")), _mhz(dev.get("rx"))
    pep = dev.get("pep") or 0
    split = tx is not None and rx is not None and abs(tx - rx) >= 0.3
    return pep >= 5 or split


class BrandmeisterDirectory:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = None
        self._at = 0.0
        self._tg = {}          # id -> (time, list)
        self._names = None
        self._names_at = 0.0

    def repeaters(self):
        """Compact list of repeater-like Brandmeister devices with coordinates and a
        recent sighting, or None if the directory has never been fetched."""
        now = time.time()
        with self._lock:
            if self._rows is not None and now - self._at < DEVICES_TTL:
                return self._rows
        try:
            raw = json.loads(_get(BM_DEVICES_URL, timeout=90))
        except Exception as e:
            print(f"[nearby] Brandmeister directory fetch failed: {type(e).__name__}: {e}")
            with self._lock:
                return self._rows                        # keep serving a stale copy if we have one
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - STALE_DAYS * 86400))
        rows = []
        for d in raw:
            try:
                lat, lng = float(d.get("lat")), float(d.get("lng"))
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
                continue
            if (d.get("last_seen") or "")[:10] < cutoff:
                continue
            if not is_repeater(d):
                continue
            rows.append({"id": d.get("id"), "callsign": (d.get("callsign") or "").strip(), "tx": _mhz(d.get("tx")),
                         "rx": _mhz(d.get("rx")), "colorcode": d.get("colorcode"), "city": (d.get("city") or "").strip(),
                         "lat": lat, "lng": lng, "pep": d.get("pep") or 0})
        with self._lock:
            self._rows, self._at = rows, now
        return rows

    def talkgroups(self, device_id):
        """Static talkgroups for one device as [{"talkgroup", "slot", "name"}], or None
        on failure. An empty list is a real answer (nothing static registered)."""
        try:
            device_id = int(device_id)
        except (TypeError, ValueError):
            return None
        now = time.time()
        with self._lock:
            hit = self._tg.get(device_id)
            if hit and now - hit[0] < TG_TTL:
                return hit[1]
        try:
            raw = json.loads(_get(BM_TG_URL.format(device_id), timeout=20))
        except Exception as e:
            print(f"[nearby] Brandmeister talkgroups for {device_id} failed: {type(e).__name__}: {e}")
            return None
        names = self._tg_names()
        out = []
        for t in raw if isinstance(raw, list) else []:
            tg = str(t.get("talkgroup", "")).strip()
            if tg:
                out.append({"talkgroup": tg, "slot": str(t.get("slot", "")), "name": names.get(tg, "")})
        out.sort(key=lambda x: (x["slot"], int(x["talkgroup"]) if x["talkgroup"].isdigit() else 0))
        with self._lock:
            self._tg[device_id] = (now, out)
        return out

    def _tg_names(self):
        now = time.time()
        with self._lock:
            if self._names is not None and now - self._names_at < TG_NAMES_TTL:
                return self._names
        try:
            names = {str(k): str(v) for k, v in json.loads(_get(BM_TG_NAMES_URL, timeout=30)).items()}
        except Exception:
            with self._lock:
                return self._names or {}
        with self._lock:
            self._names, self._names_at = names, now
        return names


_FREQ_RE = re.compile(r"^\s*(\d{2,3}\.\d+)")


def parse_asl_db(text):
    """`node|callsign|freq|location` lines -> {CALLSIGN: [(node, freq_mhz or None, location)]}.
    Lines that aren't a numeric node are skipped."""
    by_call = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 4 or not parts[0].strip().isdigit():
            continue
        m = _FREQ_RE.match(parts[2])
        by_call.setdefault(parts[1].strip().upper(), []).append(
            (parts[0].strip(), float(m.group(1)) if m else None, parts[3].strip()))
    return by_call


def match_asl_node(by_call, callsign, freq_hz):
    """The AllStar node number for a repeater, matched by callsign and frequency
    (within 1.5 kHz). None when ambiguous or unknown -- a wrong node number would
    connect someone to the wrong place, so no match beats a guess."""
    cands = by_call.get((callsign or "").strip().upper())
    if not cands or not freq_hz:
        return None
    mhz = freq_hz / 1e6
    hits = [c for c in cands if c[1] is not None and abs(c[1] - mhz) < 0.0015]
    return hits[0] if len(hits) == 1 else None


class AslDirectory:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_call = None
        self._at = 0.0

    def by_call(self):
        now = time.time()
        with self._lock:
            if self._by_call is not None and now - self._at < ASL_TTL:
                return self._by_call
        try:
            db = parse_asl_db(_get(ASL_DB_URL, timeout=60).decode("utf-8", "ignore"))
        except Exception as e:
            print(f"[nearby] AllStarLink node database fetch failed: {type(e).__name__}: {e}")
            with self._lock:
                return self._by_call
        with self._lock:
            self._by_call, self._at = db, now
        return db
