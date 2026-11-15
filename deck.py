"""Stream Deck Neo pages, driven through Bitfocus Companion.

Companion's generic HTTP module can send requests but can't poll a URL into
a button's look, so the direction is reversed: this module PAINTS the keys
through Companion's own HTTP remote-control API
(POST /api/location/<page>/<row>/<column>/style with {text, color, bgcolor,
size}; rows/columns 0-based, from Companion's http-remote-control.md), and
the keys only report presses back here (/api/deck/key/<n>/down|up and
/api/deck/page/next|prev). So Companion holds ten plain buttons on ONE page
-- the Neo's 8 keys (rows 0-1, columns 0-3) and its two touch points (row 2,
columns 0 and 3, per the Neo model in @elgato-stream-deck/core) -- and this
module flips between its own four pages by repainting them.

The info bar is the Neo's LCD segment at row 2, columns 1-2; Companion
shows the buttons at those two cells there (page title on the left, the
page's live reading on the right).

Actions reuse the dashboard's own routes (rig_tune, asl_connect,
brandmeister_talkgroup, ircddb_link/unlink, sstv/control,
wfweb_power_action) through `post`, so validation and the control gate are
exactly the same as from the browser. Never raises out of its public
methods (same contract as every integration here).
"""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:          # no tzdata: W1AW key just shows nothing scheduled
    ZoneInfo = None
    _ET = None

from storage import load_asl_favorites, load_bm_tg_favorites, load_hotspots, load_ircddb_favorites, load_settings

PAGES = ["HF", "ASL", "DV", "STATUS"]
PAGE_TITLES = {"HF": "HF rig", "ASL": "AllStar", "DV": "Digital voice", "STATUS": "Station"}
PUSH_INTERVAL = 1.0
FULL_REPAINT_SEC = 30          # repaint everything now and then, so a Companion restart recovers
ASL_MODES = ["connect", "monitor", "localmonitor"]
ASL_MODE_LABEL = {"connect": "CONNECT", "monitor": "MONITOR", "localmonitor": "LOCAL MON"}

# Key colours (Companion buttons are a flat fill, so state is the background).
C = {
    "idle": ("#ffffff", "#000000"),
    "live": ("#bff7ee", "#0b5a52"),     # on the air / listening
    "on": ("#c9f7da", "#14632f"),       # linked / on frequency / on
    "keyed": ("#ffffff", "#a3161f"),    # talking now
    "warn": ("#1b1600", "#f0c419"),     # needs a look / hold in progress
    "danger": ("#ff8a90", "#2a0709"),   # destructive or offline
    "off": ("#6b737d", "#000000"),      # nothing to do
}

# W1AW (Eastern, Mon-Fri), CWops CWT and K1USN SST (UTC) -- same tables as
# the CW Copy card's strip (dashboard.html renderCwAir); keep them in sync.
W1AW_BANDS = {"160": 1802500, "80": 3581500, "40": 7047500, "20": 14047500, "17": 18077500, "15": 21067500, "10": 28067500}
CALL_RE = re.compile(r"^([A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9][A-Z]{1,4}$")


def _w1aw_events(now):
    if _ET is None:
        return []
    out = []
    today = datetime.fromtimestamp(now, _ET).date()
    for i in range(-1, 8):
        d = today + timedelta(days=i)
        dow = d.isoweekday()            # 1 = Monday
        if dow > 5:
            continue
        mwf = dow in (1, 3, 5)
        fast, slow = ("Fast code", "35-10 wpm"), ("Slow code", "5-15 wpm")
        bull = ("Bulletin", "18 wpm")
        for h, (name, det), dur in ((16, fast if mwf else slow, 60), (17, bull, 30), (19, slow if mwf else fast, 60),
                                    (20, bull, 30), (22, fast if mwf else slow, 60), (23, bull, 30)):
            start = datetime(d.year, d.month, d.day, h, 0, tzinfo=_ET).timestamp()
            if start + dur * 60 > now:
                out.append({"name": name, "detail": det, "start": start, "end": start + dur * 60})
    return sorted(out, key=lambda e: e["start"])


def _mhz(hz):
    return f"{hz / 1e6:.4f}".rstrip("0").rstrip(".") if hz else ""


def _ago(sec):
    sec = int(sec)
    if sec < 3600:
        return f"{max(1, sec // 60)} m"
    return f"{sec // 3600} h {sec % 3600 // 60} m"


class DeckController:
    def __init__(self, get, post):
        self._get = get                 # path -> parsed JSON or None
        self._post = post               # (path, body) -> parsed JSON or None
        self._lock = threading.Lock()
        self._enabled = False
        self._url = ""
        self._cpage = 1
        self._page = 0
        self._asl_mode = "connect"
        self._multi = False
        self._sent = {}                 # (row, col) -> style dict last pushed
        self._full_at = 0.0
        self._holds = {}                # key index -> {"t": down time, "fired": bool}
        self._flash = {}                # key index -> (until, text, colour) brief result
        self._keys = []                 # current page's key specs (for presses)
        self._last_ok = None
        self._last_error = None
        self._wake = threading.Event()
        self._started = False

    def start(self):
        """Started from app.main() with the other background loops -- never
        at import, when the routes it calls aren't registered yet."""
        if not self._started:
            self._started = True
            threading.Thread(target=self._loop, daemon=True).start()

    # --- public -----------------------------------------------------------
    def configure(self, enabled, url, companion_page):
        with self._lock:
            self._enabled = bool(enabled)
            self._url = (url or "").strip().rstrip("/")
            if self._url and not re.match(r"^https?://", self._url):
                self._url = "http://" + self._url
            try:
                self._cpage = max(1, int(companion_page))
            except (TypeError, ValueError):
                self._cpage = 1
            self._sent = {}
        self._wake.set()

    def status(self):
        with self._lock:
            return {"enabled": self._enabled, "url": self._url, "companion_page": self._cpage,
                    "page": PAGES[self._page], "last_ok": self._last_ok, "error": self._last_error}

    def step_page(self, delta):
        with self._lock:
            self._page = (self._page + delta) % len(PAGES)
            self._holds = {}
            self._flash = {}
            self._keys = []         # presses are ignored until the new page is painted
        self._wake.set()
        return PAGES[self._page]

    def key(self, n, phase):
        """A key went down or up. Taps fire on 'down' (snappier); keys with
        a hold time fire once held that long, and do nothing if let go early."""
        try:
            with self._lock:
                spec = self._keys[n] if 0 <= n < len(self._keys) else None
                if spec is None or not spec.get("act"):
                    return
                if phase == "down":
                    if spec.get("hold"):
                        self._holds[n] = {"t": time.time(), "fired": False, "spec": spec}
                        self._wake.set()
                        return
                elif phase == "up":
                    h = self._holds.pop(n, None)
                    if h and not h["fired"]:
                        self._flash[n] = (time.time() + 1.5, "Hold to\nconfirm", "warn")
                    self._wake.set()
                    return
                else:
                    return
            threading.Thread(target=self._run, args=(n, spec), daemon=True).start()
        except Exception:
            pass

    # --- loop ---------------------------------------------------------------
    def _loop(self):
        while True:
            self._wake.wait(PUSH_INTERVAL)
            self._wake.clear()
            try:
                self._tick()
            except Exception as e:
                with self._lock:
                    self._last_error = f"{type(e).__name__}: {e}"

    def _tick(self):
        with self._lock:
            if not self._enabled or not self._url:
                return
            page = PAGES[self._page]
            # holds that have reached their time fire now
            due = [(n, h["spec"]) for n, h in self._holds.items()
                   if not h["fired"] and time.time() - h["t"] >= h["spec"]["hold"]]
            for n, _ in due:
                self._holds[n]["fired"] = True
        for n, spec in due:
            threading.Thread(target=self._run, args=(n, spec), daemon=True).start()

        state = self._state()
        keys, info = getattr(self, "_page_" + page.lower())(state)
        keys = (keys + [{}] * 8)[:8]
        now = time.time()
        with self._lock:
            self._keys = keys
            for n, h in self._holds.items():
                if not h["fired"]:
                    left = max(0.0, h["spec"]["hold"] - (now - h["t"]))
                    keys[n] = dict(keys[n], label="HOLD", value=f"{left:.1f} s", sub=keys[n].get("label", ""), state="warn")
            for n, (until, text, colour) in list(self._flash.items()):
                if now > until:
                    del self._flash[n]
                else:
                    keys[n] = dict(keys[n], text=text, state=colour)
            full = now - self._full_at > FULL_REPAINT_SEC
            if full:
                self._full_at = now
                self._sent = {}
        styles = {}
        for i, k in enumerate(keys):
            styles[(i // 4, i % 4)] = self._style(k)
        # The Neo's info bar is two Companion cells (row 2, columns 1 and 2).
        styles[(2, 1)] = {"text": f"{PAGE_TITLES[page]}\n{self._page + 1}/{len(PAGES)}", "color": "#ffb648",
                          "bgcolor": "#000000", "size": 14}
        styles[(2, 2)] = {"text": info, "color": "#bfeee6", "bgcolor": "#000000", "size": 14}
        styles[(2, 0)] = {"text": "<", "color": "#9aa2ad", "bgcolor": "#000000", "size": 14}
        styles[(2, 3)] = {"text": ">", "color": "#9aa2ad", "bgcolor": "#000000", "size": 14}
        for loc, st in styles.items():
            if self._sent.get(loc) == st:
                continue
            if self._push(loc, st):
                self._sent[loc] = st

    def _style(self, k):
        fg, bg = C.get(k.get("state") or "idle", C["idle"])
        if "text" in k:
            text = k["text"]
        elif k:
            text = "\n".join(x for x in (k.get("label", ""), k.get("value", ""), k.get("sub", "")) if x)
        else:
            text = ""
        return {"text": text, "color": fg, "bgcolor": bg, "size": 14}

    def _push(self, loc, style):
        with self._lock:
            url, cpage = self._url, self._cpage
        req = urllib.request.Request(
            f"{url}/api/location/{cpage}/{loc[0]}/{loc[1]}/style",
            data=json.dumps(style).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
            with self._lock:
                self._last_ok, self._last_error = time.time(), None
            return True
        except Exception as e:
            with self._lock:
                self._last_error = f"Can't reach Companion at {url} ({e})"
            return False

    def _run(self, n, spec):
        try:
            ok, msg = spec["act"]()
        except Exception as e:
            ok, msg = False, str(e)
        with self._lock:
            self._flash[n] = (time.time() + (1.2 if ok else 3.0),
                              "OK" if ok else "FAILED\n" + (msg or "")[:24], "on" if ok else "danger")
        self._wake.set()

    # --- state --------------------------------------------------------------
    def _state(self):
        s = {"data": self._get("/api/data") or [], "settings": load_settings()}
        wp = self._get("/api/wfweb_power") or {}
        cw = self._get("/api/cw_copy") or {}
        s["wfweb"] = wp
        s["cw"] = cw
        s["freq_hz"] = (wp.get("connected") and wp.get("freq_hz")) or cw.get("freq_hz")
        s["mode"] = str((wp.get("connected") and wp.get("mode")) or cw.get("mode") or "").upper()
        return s

    def _tune(self, hz, mode):
        def act():
            r = self._post("/api/rig_tune", {"freq_hz": hz, "mode": mode}) or {}
            return bool(r.get("ok")), r.get("message", "")
        return act

    @staticmethod
    def _near(s, hz, tol=500):
        f = s.get("freq_hz")
        return bool(f) and abs(f - hz) <= tol

    # --- page 1: HF rig -----------------------------------------------------
    def _page_hf(self, s):
        now = time.time()
        rig_ok = bool(s["settings"].get("rig_control_enabled"))
        band = s["settings"].get("deck_w1aw_band") or "80"
        w1hz = W1AW_BANDS.get(band, W1AW_BANDS["80"])
        ev = _w1aw_events(now)
        e = ev[0] if ev else None
        if e and e["start"] <= now:
            w1 = {"label": "W1AW " + e["name"], "value": _mhz(w1hz), "sub": "on now",
                  "state": "on" if self._near(s, w1hz) else "live", "act": self._tune(w1hz, "CW") if rig_ok else None}
        elif e:
            if e["start"] - now < 12 * 3600:
                when = "in " + _ago(e["start"] - now)
            else:
                when = datetime.fromtimestamp(e["start"], _ET).strftime("%a %-I%p" if os.name != "nt" else "%a %#I%p").lower()
                when = when[:1].upper() + when[1:] + " ET"
            w1 = {"label": "W1AW next", "value": e["name"], "sub": when,
                  "act": self._tune(w1hz, "CW") if rig_ok else None}
        else:
            w1 = {"label": "W1AW", "value": "-", "state": "off"}
        slot = int(now) % 180 // 10
        beacons = ["4U1UN", "VE8AT", "W6WX", "KH6RS", "ZL6B", "VK6RBP", "JA2IGY", "RR9O", "VR2B",
                   "4S7B", "ZS6DN", "5Z4B", "4X6TU", "OH2B", "CS3B", "LU4AA", "OA4B", "YV5B"]
        bcn = {"label": "Beacons", "value": "14.100", "sub": beacons[slot],
               "state": "on" if self._near(s, 14100000) else "idle", "act": self._tune(14100000, "CW") if rig_ok else None}
        ft20 = {"label": "FT8 20m", "value": "14.074", "sub": "on freq" if self._near(s, 14074000) else "USB",
                "state": "on" if self._near(s, 14074000) else "idle", "act": self._tune(14074000, "USB") if rig_ok else None}
        ft40 = {"label": "FT8 40m", "value": "7.074", "sub": "on freq" if self._near(s, 7074000) else "USB",
                "state": "on" if self._near(s, 7074000) else "idle", "act": self._tune(7074000, "USB") if rig_ok else None}
        f, m = s.get("freq_hz"), s.get("mode") or ""
        cwkey = {"label": "CW mode", "value": "IN CW" if "CW" in m else ((m + ">CW") if (m and f) else "-"), "sub": _mhz(f),
                 "state": "on" if "CW" in m else "idle",
                 "act": self._tune(f, "CW") if (rig_ok and f and "CW" not in m) else None}
        cw = s["cw"]
        heard = self._cw_heard(cw.get("lines") or [])
        heardkey = {"label": "CW heard", "value": heard or "-", "sub": cw.get("state", "off"),
                    "state": "live" if cw.get("state") == "decoding" else "idle"}
        sstv = self._get("/api/sstv") or {}
        rx, ctl = sstv.get("rx") or {}, sstv.get("ctl") or {}
        if rx.get("receiving"):
            r = rx["receiving"]
            sk = {"label": "SSTV rx", "value": r.get("label", "")[:9], "sub": f"{int(r.get('progress', 0) * 100)}%",
                  "state": "warn", "act": self._sstv("stop")}
        elif ctl.get("state") == "active":
            sk = {"label": "SSTV", "value": "LISTENING", "sub": "tap: stop", "state": "live", "act": self._sstv("stop")}
        else:
            sk = {"label": "SSTV", "value": _mhz((sstv.get("config") or {}).get("freq_hz")), "sub": "tap: listen",
                  "act": self._sstv("listen_now") if rig_ok else None}
        wp = s["wfweb"]
        pw = str(wp.get("power_state")).lower()        # wfweb sends true/false; tolerate "on"/"off" too
        if not s["settings"].get("wfweb_power_enabled") or not wp.get("connected"):
            pk = {"label": "Rig power", "value": "-", "sub": "wfweb off", "state": "off"}
        elif pw in ("on", "true", "1"):
            pk = {"label": "Rig power", "value": "ON", "sub": "hold: off", "state": "on", "hold": 1.0,
                  "act": self._power("power_off")}
        else:
            pk = {"label": "Rig power", "value": "OFF", "sub": "tap: on", "act": self._power("power_on")}
        info = f"{_mhz(f)}\n{m}".strip() if f else "Rig not\nconnected"
        return [w1, bcn, ft20, sk, cwkey, heardkey, ft40, pk], info

    @staticmethod
    def _cw_heard(lines):
        counts, order = {}, []
        for l in lines[-20:]:
            w = (l.get("text") or "").split(" ")
            for i, tok in enumerate(w):
                t = re.sub(r"/[A-Z0-9]+$", "", tok)
                if CALL_RE.match(t):
                    counts[t] = counts.get(t, 0) + (2 if i > 0 and w[i - 1] == "DE" else 1)
                    order.append(t)
        for c in reversed(order):
            if counts[c] >= 2:
                return c
        return ""

    def _sstv(self, action):
        def act():
            r = self._post("/api/sstv/control", {"action": action}) or {}
            return bool(r.get("ok")), r.get("message", "")
        return act

    def _power(self, action):
        def act():
            r = self._post("/api/wfweb_power_action", {"action": action}) or {}
            return bool(r.get("ok")), r.get("message", "")
        return act

    # --- page 2: AllStar ----------------------------------------------------
    def _page_asl(self, s):
        hs = next((h for h in s["data"] if h.get("type") == "asl3"), None)
        if hs is None:
            return [{"label": "AllStar", "value": "none", "sub": "no ASL3 node", "state": "off"}], "no ASL3 hotspot"
        links = {str(l.get("node")): l for l in (hs.get("asl_linked_nodes") or [])}
        favs = [f for f in load_asl_favorites() if f.get("pinned", True)][:5]
        keys = []
        for f in favs:
            node = str(f.get("node"))
            l = links.get(node)
            if l and l.get("keyed"):
                st, sub = "keyed", "keyed"
            elif l and l.get("mode") == "L":
                st, sub = "live", "local mon"
            elif l and l.get("mode") == "R":
                st, sub = "live", "monitor"
            elif l and l.get("mode") == "C":
                st, sub = "warn", "connecting"
            elif l:
                st, sub = "on", "linked"
            else:
                st, sub = "idle", (f.get("label") or "")[:10]
            keys.append({"label": (f.get("label") or "Fav")[:10] if l else "Fav", "value": node, "sub": sub,
                         "state": st, "act": self._asl_toggle(hs, node, bool(l), links, favs)})
        keys = (keys + [{}] * 5)[:5]
        keys.append({"label": "Mode", "value": ASL_MODE_LABEL[self._asl_mode], "sub": "tap: change",
                     "state": "idle" if self._asl_mode == "connect" else "live", "act": self._cycle_mode})
        keys.append({"label": "Multi", "value": "ON" if self._multi else "OFF",
                     "sub": "keep others" if self._multi else "one at a time",
                     "state": "on" if self._multi else "idle", "act": self._toggle_multi})
        keys.append({"label": "Disc. all", "value": f"{len(links)} LINKS", "sub": "hold 2 s",
                     "state": "danger" if links else "off", "hold": 2.0,
                     "act": self._asl_action(hs, "0", "disconnect_all") if links else None})
        keyed = next((n for n, l in links.items() if l.get("keyed")), None)
        info = f"{len(links)} links" + (f"\n{keyed} keyed" if keyed else "")
        return keys, info

    def _asl_action(self, hs, node, action):
        def act():
            r = self._post("/api/asl_connect", {"id": hs["id"], "node": node, "action": action}) or {}
            return bool(r.get("success")), r.get("message", "")
        return act

    def _asl_toggle(self, hs, node, linked, links, favs):
        def act():
            if linked:
                return self._asl_action(hs, node, "disconnect")()
            if self._asl_mode == "connect" and not self._multi:
                fav_nodes = {str(f.get("node")) for f in favs}
                for other in [n for n in links if n in fav_nodes]:
                    self._asl_action(hs, other, "disconnect")()
            return self._asl_action(hs, node, self._asl_mode)()
        return act

    def _cycle_mode(self):
        self._asl_mode = ASL_MODES[(ASL_MODES.index(self._asl_mode) + 1) % len(ASL_MODES)]
        return True, ""

    def _toggle_multi(self):
        self._multi = not self._multi
        return True, ""

    # --- page 3: digital voice ----------------------------------------------
    def _page_dv(self, s):
        hs = next((h for h in s["data"] if h.get("type", "wpsd") == "wpsd"
                   and (h.get("brandmeister_id") or h.get("ircddb_reflector") is not None)), None) \
            or next((h for h in s["data"] if h.get("type", "wpsd") == "wpsd"), None)
        if hs is None:
            return [{"label": "Digital", "value": "none", "sub": "no WPSD hotspot", "state": "off"}], "no WPSD hotspot"
        cfg = next((h for h in load_hotspots() if h.get("id") == hs["id"]), {})
        keys = []
        static = {int(t.get("talkgroup")): t for t in (hs.get("bm_static_tgs") or []) if str(t.get("talkgroup", "")).isdigit()}
        bm_ok = bool(hs.get("brandmeister_id")) and bool((s["settings"].get("brandmeister_api_key") or "").strip())
        for f in load_bm_tg_favorites()[:3]:
            try:
                tg = int(f.get("tg"))
            except (TypeError, ValueError):
                continue
            linked = tg in static
            keys.append({"label": "BM TG", "value": str(tg),
                         "sub": f"linked TS{static[tg].get('slot')}" if linked else (f.get("label") or "")[:10],
                         "state": "on" if linked else "idle",
                         "act": self._bm(hs, tg, static[tg].get("slot") if linked else (f.get("slot") or 2),
                                         "unlink" if linked else "link") if bm_ok else None})
        keys = (keys + [{}] * 3)[:3]
        keys.append({"label": "BM", "value": "UNLINK", "sub": f"{len(static)} static · hold",
                     "state": "danger" if static else "off", "hold": 2.0,
                     "act": self._bm_unlink_all(hs, static) if (static and bm_ok) else None})
        cur = (hs.get("ircddb_reflector") or "").strip()
        ddb_ok = bool(cfg.get("ircddb_enabled"))
        for f in load_ircddb_favorites()[:2]:
            ref = (f.get("reflector") or "").strip()
            linked = bool(cur) and cur.replace(" ", "").upper() == ref.replace(" ", "").upper()
            keys.append({"label": "D-STAR", "value": ref, "sub": "linked" if linked else "tap: link",
                         "state": "on" if linked else "idle",
                         "act": (self._ddb_unlink(hs, ref) if linked else self._ddb_link(hs, ref)) if ddb_ok else None})
        keys = (keys + [{}] * 6)[:6]
        ysf = hs.get("ysf_reflector")
        keys.append({"label": "YSF", "value": (ysf or "-")[:9], "sub": "read only", "state": "on" if ysf else "off"})
        if hs.get("is_active") and hs.get("active_call"):
            onair = {"label": "On air", "value": hs["active_call"], "sub": self._mode_txt(hs),
                     "state": "keyed"}
            info = f"{hs['active_call']}\n{self._mode_txt(hs)}"
        else:
            onair = {"label": "On air", "value": "-", "sub": hs.get("name", "")[:10], "state": "off"}
            info = (hs.get("name", "") or "")[:12] + "\nquiet"
        keys.append(onair)
        return keys, info

    def _bm(self, hs, tg, slot, action):
        def act():
            r = self._post("/api/brandmeister_talkgroup", {"id": hs["id"], "tg": tg, "slot": slot, "action": action}) or {}
            return bool(r.get("success")), r.get("message", "")
        return act

    def _bm_unlink_all(self, hs, static):
        def act():
            bad = [str(tg) for tg, t in static.items() if not self._bm(hs, tg, t.get("slot", 0), "unlink")()[0]]
            return (not bad), ("failed: " + ", ".join(bad)) if bad else ""
        return act

    def _ddb_link(self, hs, ref):
        def act():
            r = self._post("/api/ircddb_link", {"id": hs["id"], "reflector": ref}) or {}
            return bool(r.get("success")), r.get("message", "")
        return act

    def _ddb_unlink(self, hs, ref):
        def act():
            st = self._get(f"/api/ircddb_status/{hs['id']}") or {}
            want = ref.replace(" ", "").upper()
            link = next((l for l in st.get("links") or [] if (l.get("callsign") or "").replace(" ", "").upper() == want), None)
            proto = link.get("protocol_index", 0) if link else 0
            r = self._post("/api/ircddb_unlink", {"id": hs["id"], "reflector": ref, "protocol": proto}) or {}
            return bool(r.get("success")), r.get("message", "")
        return act

    # --- page 4: station status -----------------------------------------------
    @staticmethod
    def _mode_txt(hs):
        for v in (hs.get("talkgroup"), hs.get("mode")):
            if v and str(v).upper() not in ("N/A", "NONE", "IDLE"):
                return str(v)
        return ""

    def _page_status(self, s):
        now = time.time()
        keys = []
        for hs in s["data"][:4]:
            name = (hs.get("name") or hs.get("ip") or "")[:10]
            if hs.get("status") == "Offline":
                since = hs.get("offline_since")
                keys.append({"label": name, "value": "AWAY" if hs.get("portable") else "OFFLINE",
                             "sub": _ago(now - since) if since else "", "state": "off" if hs.get("portable") else "danger"})
            elif hs.get("is_active") and hs.get("active_call"):
                keys.append({"label": name, "value": hs["active_call"], "sub": self._mode_txt(hs), "state": "keyed"})
            elif hs.get("type") == "asl3":
                n = len(hs.get("asl_linked_nodes") or [])
                keys.append({"label": name, "value": str(hs.get("asl_node") or "ASL3"), "sub": f"{n} links",
                             "state": "on" if n else "idle"})
            else:
                keys.append({"label": name, "value": "IDLE", "sub": self._mode_txt(hs)})
        keys = (keys + [{}] * 4)[:4]
        sat = self._get("/api/satellites") or {}
        nxt = next((p for p in sat.get("passes") or [] if p.get("los_epoch", 0) > now), None)
        if nxt:
            on = nxt["aos_epoch"] <= now
            keys.append({"label": "Next pass", "value": (nxt.get("name") or "")[:9],
                         "sub": ("now" if on else "in " + _ago(nxt["aos_epoch"] - now)) + f" · {int(nxt.get('max_elevation', 0))}°",
                         "state": "live" if on else "idle"})
        else:
            keys.append({"label": "Next pass", "value": "-", "state": "off"})
        hf = self._get("/api/hf_conditions") or {}
        try:
            local = datetime.now(ZoneInfo(s["settings"].get("station_tz") or "UTC"))
        except Exception:
            local = datetime.now()
        day = 6 <= local.hour < 18
        b20 = next((b for b in hf.get("bands") or [] if "20m" in (b.get("name") or "")), None)
        cond = str((b20 or {}).get("day" if day else "night") or "-")
        keys.append({"label": "HF 20m", "value": cond.upper(), "sub": f"K={hf.get('k_index', '-')}",
                     "state": "on" if cond.lower() == "good" else ("warn" if cond.lower() == "poor" else "idle")})
        utc = datetime.now(timezone.utc)
        keys.append({"label": "UTC", "value": utc.strftime("%H:%M"), "sub": utc.strftime("%a %d %b")})
        keys.append({"label": "Local", "value": local.strftime("%H:%M"), "sub": local.strftime("%a")})
        online = sum(1 for h in s["data"] if h.get("status") != "Offline")
        active = sum(1 for h in s["data"] if h.get("is_active") and h.get("status") != "Offline")
        return keys, f"{online}/{len(s['data'])} online\n{active} active"
