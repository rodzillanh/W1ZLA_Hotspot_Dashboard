"""Shortwave broadcast schedules for the Shortwave card: what's on the air
now, what's starting soon, and how likely each one is to be heard at the
station's own grid square.

Two sources, both free and no-auth, both fetched and checked live
(2026-10) before this was written:

* HFCC (hfcc.org) -- the schedule broadcasters register themselves each
  season. `http://www.hfcc.org/data/<s>/<s>allx2.zip` (`s` = e.g. "b26";
  the `https://www.` URL failed from the dev box, plain http worked) holds
  `<S>all00.TXT`, a fixed-column ITU-format schedule, plus reference
  tables: broadcas.txt (broadcaster names), site.txt (site name + lat/lon
  as 43N17 077E00), language.txt (3-letter codes). Column offsets below
  come from the file's own header ruler, not guessed. DAYS uses 1=Sunday:
  checked against EiBi's own "Tu-Sa" for Radio Exterior de Espana's
  1900-0400 "23456" entry (a Mon-Fri evening start, Tue-Sat after 0000).
* EiBi (eibispace.de, Eike Bierwirth) -- a volunteer list that fills gaps
  HFCC misses (Radio Habana Cuba, most US private stations' friendly
  names, pirates). `http://www.eibispace.de/dx/sked-<s>.csv`, semicolon
  separated: kHz;Time(UTC);Days;ITU;Station;Lng;Target;Remarks;P;Start;Stop.
  README.TXT has the language-code, target-code and transmitter-site
  tables (site lines like "q-Monticello, ME (WBCQ) 46N20'30"-67W49'40"").

A new season's file can appear days after the season starts (EiBi's
sked-b26.csv was still a 404 on 2026-10-25), so each source falls back to
the previous season until the new one exists.

"Aimed at North America": HFCC target zones are CIRAF numbers. A web
reference (UDXF's ITU-sourced country list) gives 4 = central-eastern
Canada, 8 = eastern US, 9 = eastern Canada; the data itself is consistent
with 1-11 covering North America and the Caribbean (China Radio
International's Havana relay at azimuth 10 deg targets zone 8, at 305 deg
targets 6 and 7). Zones 1-11 are treated as North America. Not verified
against the ITU's own zone map. The antenna AZIMUTH column is deliberately
not used: 0 there also means an omnidirectional antenna (Chinese domestic
stations at Lhasa/Urumqi, "0" with antenna type 925), which first made
them look aimed straight at New England.

HFCC and EiBi rows merge only when the start time matches within 15
minutes, the transmitter country agrees (HFCC ADM vs. EiBi's host country,
"/USA" in a relay's remark), and known sites are within 300 mi -- WRMI and
WBCQ air several unrelated programmes on one frequency through the day.

"Likely to hear" is a heuristic score, not a propagation model: aimed
here, transmitter power, distance (including the night-time skip zone on
41m and up), whether the path is dark or daylit (sun elevation at points
along the great circle), the HF Conditions card's band rating, and how
often the operator has marked the station as heard. Every factor adds a
plain-language reason the card shows in a tooltip.

Never raises out of its public methods, same contract as every other
client here.
"""
import csv
import io
import json
import math
import os
import re
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import config

_UA = "Mozilla/5.0 (W1ZLA Hotspot Dashboard; shortwave schedules)"
_TIMEOUT = 40
_REFRESH_SEC = 24 * 3600
_DIR = os.path.join(config.CONFIG_DIR, "shortwave")
_HEARD_FILE = os.path.join(config.CONFIG_DIR, "shortwave_heard.json")

HFCC_URL = "http://www.hfcc.org/data/{s}/{s}allx2.zip"
EIBI_URL = "http://www.eibispace.de/dx/sked-{s}.csv"
EIBI_README_URL = "http://www.eibispace.de/dx/README.TXT"

NA_ZONES = set(range(1, 12))
NA_TARGETS = {"NAm", "ENA", "CNA", "WNA", "Am", "USA", "CAN", "Glo", "Car"}
TARGET_NAMES = {
    "Eu": "Europe", "WEu": "western Europe", "EEu": "eastern Europe", "NEu": "northern Europe",
    "SEu": "southern Europe", "Af": "Africa", "WAf": "West Africa", "EAf": "East Africa",
    "NAf": "North Africa", "SAf": "southern Africa", "ME": "the Middle East", "As": "Asia",
    "FE": "the Far East", "SEA": "Southeast Asia", "SAs": "South Asia", "Oc": "Oceania",
    "SAm": "South America", "LAm": "Latin America", "CAm": "Central America",
}
# Broadcast bands, widened to take in the US private stations that run
# just outside them (5085, 5130, 5850, 7490-7780, 9265, 9330 kHz). Leaves
# out the coast-guard / volmet / navy frequencies EiBi also lists.
BROADCAST_BANDS = [
    (2300, 2495, "120m"), (3200, 3400, "90m"), (3900, 4000, "75m"), (4750, 5140, "60m"),
    (5840, 6200, "49m"), (7200, 7800, "41m"), (9250, 9990, "31m"), (11500, 12160, "25m"),
    (13570, 13870, "22m"), (15100, 15900, "19m"), (17480, 17900, "16m"), (18900, 19020, "15m"),
    (21450, 21850, "13m"), (25600, 26100, "11m"),
]
# HFCC registers US private stations under their licensee's name; these
# are the call signs listeners know them by (codes from broadcas.txt).
HFCC_ALIASES = {
    "BCQ": "WBCQ", "TWW": "WTWW", "BWW": "WTWW", "WCR": "WWCR", "INB": "WINB", "WIB": "WINB",
    "EWN": "WEWN (EWTN)", "RNO": "WRNO", "RMI": "WRMI Radio Miami International",
    "AGM": "USAGM (VOA / Radio Martí)", "NEW": "Unnamed new broadcaster",
}
PIRATE_RANGE = (6850, 6995)
PIRATE_TUNE_KHZ = 6925
_DAYNAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def band_name(khz: float):
    for lo, hi, name in BROADCAST_BANDS:
        if lo <= khz <= hi:
            return name
    if PIRATE_RANGE[0] <= khz <= PIRATE_RANGE[1]:
        return "43m"
    return None


# ---- seasons --------------------------------------------------------------

def _last_sunday(year, month):
    d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def season_codes(now=None):
    """[current, previous] season codes, e.g. ['b26', 'a26']. A season runs
    from the last Sunday of March (A) or October (B) at 0100 UTC."""
    now = now or datetime.now(timezone.utc)
    y = now.year
    a_start = _last_sunday(y, 3).replace(hour=1)
    b_start = _last_sunday(y, 10).replace(hour=1)
    if now >= b_start:
        cur, prev = ("b", y), ("a", y)
    elif now >= a_start:
        cur, prev = ("a", y), ("b", y - 1)
    else:
        cur, prev = ("b", y - 1), ("a", y - 1)
    return [f"{l}{yy % 100:02d}" for l, yy in (cur, prev)]


# ---- geometry -------------------------------------------------------------

def _dms(deg, mins, secs, hemi):
    v = int(deg) + int(mins) / 60 + (int(secs) / 3600 if secs else 0)
    return -v if hemi in "SW" else v


_COORD_RE = re.compile(r"(\d{1,2})([NS])(\d{2})(?:'(\d{2}))?\D{0,2}?-?\s*(\d{1,3})([EW])(\d{2})(?:'(\d{2}))?")


def _parse_coord(text):
    m = _COORD_RE.search(text)
    if not m:
        return None
    return (_dms(m.group(1), m.group(3), m.group(4), m.group(2)),
            _dms(m.group(5), m.group(7), m.group(8), m.group(6)))


def _dist_bearing(lat1, lon1, lat2, lon2):
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    a = math.sin((r2 - r1) / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    dist_mi = 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dlon) * math.cos(r2)
    x = math.cos(r1) * math.sin(r2) - math.sin(r1) * math.cos(r2) * math.cos(dlon)
    return dist_mi, (math.degrees(math.atan2(y, x)) + 360) % 360


def _midpoints(lat1, lon1, lat2, lon2, n=5):
    """n points evenly along the great circle, ends included."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d < 1e-6:
        return [(lat1, lon1)]
    out = []
    for i in range(n):
        f = i / (n - 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return out


def sun_elevation(lat, lon, when):
    """Solar elevation in degrees (NOAA's simplified equations, good to a
    fraction of a degree -- plenty for "is this point dark")."""
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def _dark_fraction(lat1, lon1, lat2, lon2, when):
    pts = _midpoints(lat1, lon1, lat2, lon2)
    return sum(1 for la, lo in pts if sun_elevation(la, lo, when) < -6) / len(pts)


# ---- parsing --------------------------------------------------------------

def _zones(s):
    out = set()
    for part in s.replace(" ", "").split(","):
        if "-" in part:
            a, b = ("".join(c for c in x if c.isdigit()) for x in part.split("-", 1))
            if a and b:
                out |= set(range(int(a), int(b) + 1))
        else:
            n = "".join(c for c in part if c.isdigit())
            if n:
                out.add(int(n))
    return out


def _hhmm(s):
    s = s.strip()
    return int(s[:2]) * 60 + int(s[2:4])


def parse_hfcc(zip_bytes):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = {n.lower(): n for n in z.namelist()}

    def lines(name):
        real = names.get(name.lower())
        if not real:
            return []
        return z.read(real).decode("latin-1").splitlines()

    brc = {l[:3].strip(): l[4:].strip() for l in lines("broadcas.txt") if l and not l.startswith(";")}
    sites = {}
    for l in lines("site.txt"):
        if l.startswith(";") or len(l) < 40:
            continue
        m = re.search(r"(\d{2})([NS])(\d{2})\s+(\d{3})([EW])(\d{2})", l[38:])
        if m:
            sites[l[:3].strip()] = (l[4:34].strip(), _dms(m.group(1), m.group(3), None, m.group(2)),
                                    _dms(m.group(4), m.group(6), None, m.group(5)))
    langs = {}
    for l in lines("language.txt"):
        l = l.lstrip("﻿")
        if l and not l.startswith(";") and len(l) > 4:
            langs[l[:3].strip()] = l[4:].strip()
    sched = next((n for n in z.namelist() if re.match(r"(?i)[ab]\d\dall00\.txt$", n)), None)
    if not sched:
        return []
    out = []
    for l in z.read(sched).decode("latin-1").splitlines():
        if l.startswith(";") or len(l) < 121:
            continue
        try:
            khz = int(l[0:5])
            start, stop = _hhmm(l[6:10]), _hhmm(l[11:15])
        except ValueError:
            continue
        if l[93:95].strip() != "D":   # D = double-sideband AM; anything else (DRM etc.) isn't tunable here
            continue
        code = l[47:50].strip()
        site = sites.get(code)
        lang_raw = l[102:112].strip()
        lang_names = [langs.get(lang_raw[i:i + 3], lang_raw[i:i + 3]) for i in range(0, len(lang_raw), 3)]
        days_raw = l[72:79].strip()
        days = None
        if days_raw and days_raw != "1234567":
            days = {(int(c) + 5) % 7 for c in days_raw if c.isdigit() and c != "0"}   # 1=Sun -> 6
        try:
            kw = int(l[51:55])
        except ValueError:
            kw = None
        zones = _zones(l[16:46])
        out.append({
            "src": "HFCC", "khz": khz, "start": start, "stop": stop, "days": days, "irregular": False,
            "station": HFCC_ALIASES.get(l[117:120].strip()) or brc.get(l[117:120].strip(), l[117:120].strip()),
            "lang": ", ".join(x for x in lang_names if x) or "",
            "site": site[0] if site else code, "lat": site[1] if site else None,
            "lon": site[2] if site else None, "kw": kw, "adm": l[113:116].strip(),
            "aimed_na": bool(zones & NA_ZONES), "target": None, "kind": "broadcast",
        })
    return out


def parse_eibi_readme(text):
    """(language names, {(ITU, site code): (name, lat, lon)})."""
    langs, sites = {}, {}
    sec = None
    country = None
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if re.match(r"I\)\s+Language codes", s):
            sec = "lang"
            continue
        if re.match(r"II\)", s):
            sec = None
            continue
        if re.match(r"IV\)\s+Transmitter site codes", s):
            sec = "site"
            continue
        if sec == "site" and re.match(r"V\)", s):
            sec = None
            continue
        if sec == "lang":
            m = re.match(r"^\s{2,4}(\S{1,4})\s{2,}([^:\[]+?)(?::|\s{2,}|\[|$)", line)
            if m:
                langs[m.group(1)] = m.group(2).strip()
        elif sec == "site":
            m = re.match(r"^\s{2,4}([A-Z]{1,3}):\s+(.*)$", line)
            if m:
                country, rest = m.group(1), m.group(2)
            elif country and re.match(r"^\s{6,}\S", line):
                rest = s
            else:
                continue
            m2 = re.match(r"^([A-Za-z0-9]{1,3})-(.+)$", rest)
            code, desc = (m2.group(1), m2.group(2)) if m2 else ("", rest)
            coord = _parse_coord(desc)
            if not coord:
                continue
            name = re.split(r"\s\d{1,2}[NS]\d", desc)[0].strip().rstrip(",")
            sites.setdefault((country, code), (name, coord[0], coord[1]))
    return langs, sites


def _eibi_days(s):
    """(set of weekday ints 0=Mon, irregular) or None when the entry is a
    one-off date this card can't place ("22Jun", "Last7", ...)."""
    s = (s or "").strip()
    if not s:
        return None, False
    if s.lower().startswith("irr"):
        return None, True
    days = set()
    for part in s.split(","):
        part = part.strip()
        if re.fullmatch(r"[A-Z][a-z]-[A-Z][a-z]", part):
            a, b = part.split("-")
            if a not in _DAYNAMES or b not in _DAYNAMES:
                return "skip", False
            i, j = _DAYNAMES.index(a), _DAYNAMES.index(b)
            while True:
                days.add(i)
                if i == j:
                    break
                i = (i + 1) % 7
        elif part in _DAYNAMES:
            days.add(_DAYNAMES.index(part))
        elif re.fullmatch(r"[1-7]+", part):          # EiBi also writes 1=Mo..7=Su
            days |= {int(c) - 1 for c in part}
        else:
            return "skip", False
    return days or None, False


def parse_eibi(csv_text, langs, sites):
    out = []
    rows = csv.reader(io.StringIO(csv_text), delimiter=";")
    next(rows, None)
    for r in rows:
        if len(r) < 8:
            continue
        try:
            khz = float(r[0])
            a, b = r[1].split("-")
            start, stop = _hhmm(a), _hhmm(b)
        except (ValueError, IndexError):
            continue
        days, irregular = _eibi_days(r[2])
        if days == "skip":
            continue
        station, lang, target, remark = r[4].strip(), r[5].strip(), r[6].strip(), r[7].strip()
        itu = r[3].strip()
        low = station.lower()
        if lang == "-TS":
            if khz < 1700:
                continue                  # WWVB etc.: a time code, not something to listen to
            kind = "time"
        elif "pirate" in low or (remark == "xx" and itu == "USA"):
            kind = "pirate"
        elif lang.startswith("-"):
            continue                      # RTTY, HFDL, fax, CW markers...
        elif band_name(khz) and band_name(khz) != "43m":
            kind = "broadcast"
        else:
            continue                      # coast guard, volmet, military voice
        host, code = itu, remark
        m = re.match(r"^/([A-Z]{1,3})(?:-(\S+))?$", remark)
        if m:
            host, code = m.group(1), m.group(2) or ""
        elif not re.fullmatch(r"[A-Za-z0-9]{1,3}", remark or ""):
            code = ""
        site = sites.get((host, code)) or sites.get((host, ""))
        lang_name = ", ".join(langs.get(x.strip(), x.strip()) for x in lang.split(",") if x.strip())
        out.append({
            "src": "EiBi", "khz": khz, "start": start, "stop": stop, "days": days, "irregular": irregular,
            "station": station, "lang": lang_name, "site": site[0] if site else "",
            "lat": site[1] if site else None, "lon": site[2] if site else None, "kw": None, "host": host,
            "aimed_na": target in NA_TARGETS, "target": target, "kind": kind,
        })
    return out


def _same_broadcast(h, e):
    """HFCC and EiBi describe the same transmission: start within 15
    minutes, and (when both sites are known) transmitters within ~300 mi.
    A looser "times overlap" test wrongly merged the different programmes
    WRMI and WBCQ air on one frequency through the day."""
    d = abs(h["start"] - e["start"]) % 1440
    if min(d, 1440 - d) > 15:
        return False
    if h.get("adm") and e.get("host") and h["adm"] != e["host"]:
        return False
    if h["lat"] is not None and e["lat"] is not None:
        if _dist_bearing(h["lat"], h["lon"], e["lat"], e["lon"])[0] > 300:
            return False
    return True


def merge(hfcc, eibi):
    """One row per broadcast. HFCC keeps its site/power/azimuth; EiBi adds
    its (usually friendlier) station name and fills in what HFCC lacks.
    HFCC often lists one broadcast twice (two antennas or date ranges), so
    every matching HFCC row takes the EiBi name."""
    out = [dict(e, srcs=["HFCC"]) for e in hfcc]
    index = {}
    for i, e in enumerate(out):
        index.setdefault(int(round(e["khz"])), []).append(i)
    for e in eibi:
        if e["kind"] != "broadcast":
            out.append(dict(e, srcs=["EiBi"]))
            continue
        hits = [i for i in index.get(int(round(e["khz"])), []) if _same_broadcast(out[i], e)]
        if not hits:
            out.append(dict(e, srcs=["EiBi"]))
            continue
        for i in hits:
            h = out[i]
            if "EiBi" in h["srcs"]:
                continue
            h["srcs"] = ["HFCC", "EiBi"]
            h["station"] = e["station"] or h["station"]
            h["target"] = e["target"]
            h["aimed_na"] = h["aimed_na"] or e["aimed_na"]
            if h["lat"] is None and e["lat"] is not None:
                h["lat"], h["lon"], h["site"] = e["lat"], e["lon"], e["site"]
            if not h["lang"] or h["lang"] == "Undetermined":
                h["lang"] = e["lang"]
    return out


# ---- timing ---------------------------------------------------------------

def _on_air(e, when):
    m = when.hour * 60 + when.minute
    s, t = e["start"], e["stop"]
    if t == 0:
        t = 1440
    if s < t:
        if not (s <= m < t):
            return False
        start_day = when.weekday()
    else:
        if m >= s:
            start_day = when.weekday()
        elif m < t:
            start_day = (when.weekday() - 1) % 7
        else:
            return False
    return e["days"] is None or start_day in e["days"]


def _starts_within(e, when, minutes):
    """Minutes until this entry next starts, if within `minutes`."""
    m = when.hour * 60 + when.minute
    delta = (e["start"] - m) % 1440
    if delta == 0 or delta > minutes:
        return None
    start_day = (when + timedelta(minutes=delta)).weekday()
    if e["days"] is not None and start_day not in e["days"]:
        return None
    return delta


# ---- scoring --------------------------------------------------------------

def _hf_group(khz):
    if khz < 8000:
        return "80m-40m"
    if khz < 15000:
        return "30m-20m"
    if khz < 19500:
        return "17m-15m"
    return "12m-10m"


def score(e, qth, when, hf=None, heard=None):
    """(score 0-100, odds label, [reasons], extras dict)."""
    pts = 20.0
    why = []
    extra = {}
    khz = e["khz"]
    if e["aimed_na"]:
        pts += 25
        why.append("Aimed at North America")
    else:
        pts -= 10
        where = TARGET_NAMES.get(e.get("target") or "")
        why.append(f"Aimed at {where}" if where else "Aimed elsewhere")
        extra["aimed_elsewhere"] = where or "elsewhere"
    if e.get("kw"):
        pts += min(15.0, 6 * math.log10(max(1, e["kw"])))
        why.append(f"{e['kw']} kW")
    else:
        pts += 8
    if e["lat"] is not None and qth:
        dist, _ = _dist_bearing(e["lat"], e["lon"], qth[0], qth[1])
        extra["dist_mi"] = round(dist)
        dark = _dark_fraction(e["lat"], e["lon"], qth[0], qth[1], when)
        tod = "night" if dark >= 0.5 else "day"
        path = "path dark" if dark >= 0.8 else ("path daylit" if dark <= 0.2 else "path half dark")
        why.append(f"{round(dist):,} mi")
        why.append(path)
        if khz < 8000:
            pts += 20 if dark >= 0.8 else (5 if dark > 0.2 else (5 if dist < 300 else -20))
        elif khz < 12500:
            pts += 15 if dark >= 0.8 else 10 if dark > 0.2 else 5
        else:
            pts += 20 if dark <= 0.2 else (5 if dark < 0.8 else -20)
        if khz >= 7000 and dist < 500 and dark >= 0.5:
            pts -= 15
            why.append("inside the night-time skip zone")
        if khz < 5500 and dist > 3000:
            pts -= 10
        if dist > 7000:
            pts -= 15
        elif dist > 5000:
            pts -= 8
    else:
        tod = "night" if qth and sun_elevation(qth[0], qth[1], when) < -6 else "day"
    if hf:
        rating = next((b.get(tod) for b in hf.get("bands", []) if b.get("name") == _hf_group(khz)), None)
        if rating:
            pts += {"Good": 10, "Fair": 0, "Poor": -15}.get(rating, 0)
            why.append(f"HF Conditions: {_hf_group(khz)} {rating} at {tod}")
    if heard and heard.get("count"):
        pts += min(25, 15 + 5 * (heard["count"] - 1))
        n = heard["count"]
        why.append("you've heard it" + (f" {n}×" if n > 1 else ""))
        extra["heard"] = n
    pts = round(max(0, min(100, pts)))
    odds = "likely" if pts >= 60 else "possible" if pts >= 40 else "longshot"
    return round(pts), odds, why, extra


# ---- client ---------------------------------------------------------------

def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _heard_key(e):
    return f"{int(round(e['khz']))}|{e['station']}"


class ShortwaveClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._heard_lock = threading.Lock()
        self._entries = None
        self._meta = {}
        self._checked_at = 0
        self._refreshing = False

    # -- data files --
    def _path(self, name):
        return os.path.join(_DIR, name)

    def _get_file(self, name, url, max_age):
        """Cached file bytes, refetched when older than max_age; None if
        neither the cache nor the network has it."""
        p = self._path(name)
        fresh = os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age
        if not fresh:
            try:
                data = _fetch(url)
                if data:
                    os.makedirs(_DIR, exist_ok=True)
                    with open(p + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(p + ".tmp", p)
                    return data
            except Exception as e:
                print(f"[shortwave] fetch {url} failed: {e}")
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def _season_file(self, kind, url_fmt, ext):
        for s in season_codes():
            data = self._get_file(f"{kind}-{s}.{ext}", url_fmt.format(s=s), _REFRESH_SEC)
            if data:
                return s, data
        return None, None

    def _load(self):
        hfcc_s, hfcc_zip = self._season_file("hfcc", HFCC_URL, "zip")
        eibi_s, eibi_csv = self._season_file("eibi", EIBI_URL, "csv")
        readme = self._get_file("eibi-readme.txt", EIBI_README_URL, 7 * _REFRESH_SEC)
        hfcc, eibi = [], []
        if hfcc_zip:
            try:
                hfcc = parse_hfcc(hfcc_zip)
            except Exception as e:
                print(f"[shortwave] HFCC parse failed: {e}")
        if eibi_csv:
            try:
                langs, sites = parse_eibi_readme(readme.decode("latin-1")) if readme else ({}, {})
                eibi = parse_eibi(eibi_csv.decode("latin-1"), langs, sites)
            except Exception as e:
                print(f"[shortwave] EiBi parse failed: {e}")
        entries = merge(hfcc, eibi) if (hfcc or eibi) else None
        meta = {"hfcc_season": hfcc_s, "eibi_season": eibi_s, "current_season": season_codes()[0],
                "hfcc_count": len(hfcc), "eibi_count": len(eibi), "loaded_at": time.time()}
        return entries, meta

    def _ensure(self):
        with self._lock:
            have = self._entries is not None
            stale = time.time() - self._checked_at > _REFRESH_SEC
            if have and (not stale or self._refreshing):
                return
            if have:
                self._refreshing = True
        if have:
            threading.Thread(target=self._refresh_bg, daemon=True).start()
            return
        entries, meta = self._load()
        with self._lock:
            self._entries, self._meta, self._checked_at = entries, meta, time.time()

    def _refresh_bg(self):
        try:
            entries, meta = self._load()
            with self._lock:
                if entries is not None:
                    self._entries, self._meta = entries, meta
                self._checked_at = time.time()
        finally:
            self._refreshing = False

    # -- heard log --
    def _heard_all(self):
        try:
            with open(_HEARD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def mark_heard(self, khz, station, undo=False):
        try:
            key = f"{int(round(float(khz)))}|{station}"
            with self._heard_lock:
                data = self._heard_all()
                rec = data.get(key, {"count": 0})
                if undo:
                    rec["count"] = max(0, rec.get("count", 0) - 1)
                else:
                    rec["count"] = rec.get("count", 0) + 1
                    rec["last"] = time.time()
                if rec["count"]:
                    data[key] = rec
                else:
                    data.pop(key, None)
                os.makedirs(config.CONFIG_DIR, exist_ok=True)
                tmp = _HEARD_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, _HEARD_FILE)
                return rec.get("count", 0)
        except Exception as e:
            print(f"[shortwave] heard save failed: {e}")
            return None

    # -- public --
    def report(self, qth, hf=None, when=None, soon_minutes=180):
        """Everything the card needs, scored for `qth` (lat, lon) or None."""
        try:
            self._ensure()
            with self._lock:
                entries, meta = self._entries, dict(self._meta)
            if entries is None:
                return None
            when = when or datetime.now(timezone.utc)
            heard = self._heard_all()

            def row(e, at, starts_in=None):
                pts, odds, why, extra = score(e, qth, at, hf, heard.get(_heard_key(e)))
                band = band_name(e["khz"])
                return {
                    "khz": e["khz"], "station": e["station"], "lang": e["lang"], "site": e["site"],
                    "kw": e["kw"], "start": f"{e['start'] // 60:02d}{e['start'] % 60:02d}",
                    "stop": f"{e['stop'] // 60:02d}{e['stop'] % 60:02d}", "band": band,
                    "days": _days_label(e), "srcs": e["srcs"], "score": pts, "odds": odds,
                    "why": why, "starts_in": starts_in, "kind": e["kind"],
                    "mode": "AM", **extra,
                }

            now_rows, soon_rows, time_rows, pirate_rows = [], [], [], []
            pirate_band = None
            for e in entries:
                if e["kind"] == "pirate":
                    if PIRATE_RANGE[0] <= e["khz"] <= PIRATE_RANGE[1] and e["station"].lower().startswith("various"):
                        pirate_band = pirate_band or e
                        continue
                    if not e["aimed_na"]:
                        continue
                    on = _on_air(e, when)
                    d = None if on else _starts_within(e, when, 24 * 60)
                    r = row(e, when if d is None else when + timedelta(minutes=d), d)
                    r["on_air"] = on
                    if not on and d is None:
                        r["odds"] = None     # off until another day: a "likely" badge would mislead
                    pirate_rows.append(r)
                    continue
                if e["kind"] == "time":
                    if e["aimed_na"] and _on_air(e, when):
                        time_rows.append(row(e, when))
                    continue
                if _on_air(e, when):
                    r = row(e, when)
                    if e["aimed_na"] or r["odds"] != "longshot":
                        now_rows.append(r)
                else:
                    d = _starts_within(e, when, soon_minutes)
                    if d is not None:
                        at = when + timedelta(minutes=d)
                        r = row(e, at, d)
                        if e["aimed_na"] or r["odds"] != "longshot":
                            soon_rows.append(r)
            if pirate_band:
                pe = dict(pirate_band, khz=PIRATE_TUNE_KHZ, station="Usual pirate frequencies",
                          site="North America", irregular=True)
                r = row(pe, when)
                r.update(range_khz=[PIRATE_RANGE[0], PIRATE_RANGE[1] - 10], on_air=True,
                         note="Evenings and weekends, irregular")
                pirate_rows.insert(0, r)
            for r in pirate_rows:
                r["mode"] = "AM"
                r["alt_mode"] = "USB"
            now_rows.sort(key=lambda r: -r["score"])
            soon_rows.sort(key=lambda r: (r["starts_in"], -r["score"]))
            pirate_rows.sort(key=lambda r: (not r.get("on_air"), r.get("starts_in") or 0, -r["score"]))
            time_rows.sort(key=lambda r: r["khz"])
            _dedupe(now_rows, by_start=False)
            _dedupe(soon_rows)
            _dedupe(time_rows)
            return {
                "now": now_rows, "soon": soon_rows, "pirates": pirate_rows, "time_signals": time_rows,
                "meta": meta, "utc": when.strftime("%H%M"),
                "qth_dark": bool(qth) and sun_elevation(qth[0], qth[1], when) < -6,
            }
        except Exception as e:
            print(f"[shortwave] report failed: {e}")
            return None

    def schedule(self, qth=None):
        """Every broadcast aimed at North America, plus pirates, for the
        drawer's All stations tab (no scoring -- times only)."""
        try:
            self._ensure()
            with self._lock:
                entries = self._entries
            if entries is None:
                return None
            out = []
            for e in entries:
                if e["kind"] == "time" or not e["aimed_na"]:
                    continue
                if e["kind"] == "pirate" and e["station"].lower().startswith("various"):
                    continue
                out.append({"khz": e["khz"], "station": e["station"], "lang": e["lang"], "site": e["site"],
                            "start": f"{e['start'] // 60:02d}{e['start'] % 60:02d}",
                            "stop": f"{e['stop'] // 60:02d}{e['stop'] % 60:02d}",
                            "days": _days_label(e), "band": band_name(e["khz"]), "srcs": e["srcs"],
                            "kind": e["kind"]})
            out.sort(key=lambda r: (r["station"].lower(), r["start"], r["khz"]))
            _dedupe(out)
            return out
        except Exception as e:
            print(f"[shortwave] schedule failed: {e}")
            return None


def _days_label(e):
    if e.get("irregular"):
        return "irregular"
    if e["days"] is None:
        return ""
    d = sorted(e["days"])
    return ",".join(_DAYNAMES[i] for i in d)


def _dedupe(rows, by_start=True):
    """HFCC often registers the same broadcast twice (two antennas, two
    date ranges), and a broadcast the sources describe slightly
    differently can survive merge() as two rows. Keep one row per
    frequency+start (per frequency alone for "on air now", where only one
    thing is on a frequency at once), preferring the row both sources
    agree on. Rows arrive sorted best-score first."""
    best = {}
    order = []
    for r in rows:
        k = (round(r["khz"]), r.get("start") if by_start else None)
        if k not in best:
            best[k] = r
            order.append(k)
        elif len(r["srcs"]) > len(best[k]["srcs"]):
            best[k] = r
    rows[:] = [best[k] for k in order]
