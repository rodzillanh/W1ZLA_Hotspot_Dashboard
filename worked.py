"""Worked Before index -- one entry per base callsign, built from qsos.json.

Backs the "Worked Before" badge shown next to callsigns across the
dashboard (hotspot active caller, Spots rows, Top 5, Recent Contacts,
Notifications). Pure local data, no network: the index is rebuilt only
when qsos.json's mtime/size changes, so /api/worked is cheap to poll.

Callsigns are matched on their BASE call, so "EA8/W1ABC", "W1ABC/P",
"W1ABC/MM" and "W1ABC-9" all count as "W1ABC". A plain node number (an
ASL3 linked node with no resolved callsign) never matches anything,
since base_call() returns "" for anything without both a letter and a
digit.
"""

import os
import re
import threading
import time
from datetime import datetime, timezone

import config
from storage import load_qsos

# A callsign-shaped token: letters/digits with at least one digit that's
# followed somewhere by a letter (W1ABC, 9A2AA, 3DA0RU, VP2E). Portable
# suffixes (P, M, MM, QRP, a bare digit) and short country prefixes are
# weeded out by picking the LONGEST matching part of a "/"-separated call.
_CALL_PART_RE = re.compile(r"^[A-Z0-9]*[0-9][A-Z0-9]*[A-Z][A-Z0-9]*$")
_SSID_RE = re.compile(r"-\d+$")

HISTORY_PER_CALL = 5


def base_call(call) -> str:
    if not call:
        return ""
    c = _SSID_RE.sub("", str(call).strip().upper())
    parts = [p for p in c.split("/") if p]
    cands = [p for p in parts if _CALL_PART_RE.match(p) and re.search(r"[A-Z]", p)]
    if not cands:
        return ""
    # Longest wins; on a tie the first one ("W1ABC/K2" style) wins.
    return max(cands, key=len)


def _qso_epoch(q: dict):
    t = q.get("logged_at")
    if isinstance(t, (int, float)):
        return float(t)
    d = str(q.get("date") or "").strip()
    if len(d) == 8 and d.isdigit():
        try:
            return datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def build_index(qsos: list) -> dict:
    by_call: dict = {}
    for q in qsos:
        b = base_call(q.get("call"))
        if not b:
            continue
        e = by_call.setdefault(b, {"n": 0, "bands": set(), "cfm_bands": set(),
                                   "confirmed": False, "last": None, "_q": []})
        e["n"] += 1
        band = str(q.get("band") or "").strip().lower()
        cfm = bool(q.get("qrz_confirmed"))
        if band:
            e["bands"].add(band)
            if cfm:
                e["cfm_bands"].add(band)
        if cfm:
            e["confirmed"] = True
        t = _qso_epoch(q)
        if t is not None and (e["last"] is None or t > e["last"]):
            e["last"] = t
        e["_q"].append((t or 0, q))
    out = {}
    for b, e in by_call.items():
        recent = sorted(e["_q"], key=lambda x: x[0], reverse=True)[:HISTORY_PER_CALL]
        out[b] = {
            "n": e["n"],
            "bands": sorted(e["bands"]),
            "cfm_bands": sorted(e["cfm_bands"]),
            "confirmed": e["confirmed"],
            "last": e["last"],
            "qsos": [q for _, q in recent],
        }
    return out


_lock = threading.Lock()
_cache = {"sig": None, "index": {}, "built_at": 0.0}


def _file_sig():
    try:
        st = os.stat(config.QSOS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get_index() -> dict:
    """Cached index, rebuilt when qsos.json changes. Never raises."""
    sig = _file_sig()
    with _lock:
        if sig is not None and sig == _cache["sig"]:
            return _cache["index"]
    try:
        idx = build_index(load_qsos()) if sig is not None else {}
    except Exception as e:  # never let a bad qsos.json break the dashboard
        print(f"worked: index build failed: {e}")
        idx = {}
    with _lock:
        _cache.update(sig=sig, index=idx, built_at=time.time())
    return idx
