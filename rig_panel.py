"""Live rig state over Hamlib rigctld -- backs the Rig Panel card, the
Operating Timeline (band/mode history), and the PA-temperature
Notifications alert (all three from one poller).

Unlike rigctl.py (short-lived connect-per-tune), this holds ONE
persistent socket to rigctld and polls it on a slow cadence
(RIG_PANEL_POLL_SEC on RX, RIG_PANEL_TX_POLL_SEC while the rig reports
PTT). rigctld serializes CAT access with the operator's own WSJT-X /
logger, so the poll is deliberately gentle and only runs when a
consumer is actually enabled (the card, or the PA alert).

Every field degrades independently: a rig / Hamlib build that doesn't
support a given `l <level>` / `u <func>` answers `RPRT -<n>` and that
field is served as None -- the card shows a dash, the alert never
fires. Never raises out of its public methods.

Hamlib reply-line counts in the default (non --vfo) mode, which is what
this assumes: `f`->1, `m`->2 (mode, passband), `v`->1, `s`->2 (0/1, tx
vfo), `i`->1, `t`->1, every `l`/`u`/`y`->1. A command that errors
answers a single `RPRT -n` line regardless -- _cmd() stops early on it.
"""
import collections
import socket
import threading
import time

import config
from wsjtx import freq_to_band


class RigPanelPoller:
    def __init__(self):
        self._lock = threading.Lock()
        self._host = ""
        self._port = 4532
        self._active = False           # run the poll loop at all
        self._alert_on = False         # actually fire PA events
        self._alert_frac = 0.60        # meter fraction that counts as "high"
        self._temp_cal = None          # (deg_at_0, deg_at_1) or None
        self._gen = 0

        self._snap = {"reachable": False}
        # Poll-thread-only state (no lock): the "slow set" (PA temp, ATU/
        # NB/NR/notch/antenna) is refreshed every RIG_PANEL_SLOW_EVERY_SEC
        # and merged into every fast snapshot, so the fast poll stays ~6
        # cheap commands and freq/mode/S-meter feel responsive.
        self._slow_cache = {}
        self._slow_at = 0.0
        self._temps = collections.deque(maxlen=config.RIG_PANEL_TEMP_HISTORY)     # (epoch, frac)
        self._timeline = collections.deque(maxlen=config.RIG_PANEL_TIMELINE_MAX)  # {band, mode, start, end}
        self._pa_events = collections.deque(maxlen=50)
        self._pa_high = False
        self._pa_since = None

        try:
            import storage_notifications
            for row in storage_notifications.recent("rig_pa", 50):
                self._pa_events.append(row)
        except Exception:
            pass

    # --- config ---

    def configure(self, host, port, active, alert_on, alert_pct, temp_cal):
        cal = None
        try:
            a, b = (temp_cal or "").split(",")
            cal = (float(a), float(b))
        except (ValueError, AttributeError):
            cal = None
        try:
            frac = max(0.0, min(1.0, float(alert_pct) / 100.0))
        except (TypeError, ValueError):
            frac = 0.60
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 4532
        with self._lock:
            new = ((host or "").strip(), port, bool(active))
            self._alert_on = bool(alert_on)
            self._alert_frac = frac
            self._temp_cal = cal
            if (self._host, self._port, self._active) != new:
                self._host, self._port, self._active = new
                self._gen += 1
                if not new[2]:
                    self._snap = {"reachable": False}

    # --- public read ---

    def snapshot(self):
        with self._lock:
            s = dict(self._snap)
            s["temp_history"] = [{"at": t, "frac": f} for t, f in self._temps]
            s["timeline"] = list(self._timeline)
            s["alert_pct"] = round(self._alert_frac * 100)
            return s

    def pa_alert_events(self):
        with self._lock:
            return list(self._pa_events)[::-1]  # newest first, like the other notif sources

    def pa_alert_status(self):
        with self._lock:
            return {
                "enabled": self._alert_on,
                "connected": bool(self._snap.get("reachable")),
                "events": list(self._pa_events)[::-1],
            }

    # --- background loop ---

    def run_forever(self):
        sock = None
        my_gen = -1
        while True:
            with self._lock:
                host, port, active, gen = self._host, self._port, self._active, self._gen
            if gen != my_gen:
                sock = _close(sock)
                my_gen = gen
            if not active or not host:
                sock = _close(sock)
                time.sleep(3)
                continue
            try:
                if sock is None:
                    sock = socket.create_connection((host, port), timeout=config.RIG_PANEL_TIMEOUT)
                    sock.settimeout(config.RIG_PANEL_TIMEOUT)
                    self._slow_at = 0.0  # force a full slow-set read on the first poll of a new connection
                snap = self._poll_once(sock)
                self._apply(snap)
                nap = config.RIG_PANEL_TX_POLL_SEC if snap.get("ptt") else config.RIG_PANEL_POLL_SEC
            except OSError as e:
                sock = _close(sock)
                now = time.time()
                with self._lock:
                    self._close_open_segment(now)
                    self._snap = {"reachable": False, "error": str(e)}
                nap = config.RIG_PANEL_RECONNECT_SEC
            time.sleep(nap)

    # --- polling ---

    def _poll_once(self, sock):
        now = time.time()
        raw = {}  # exact rigctld reply per level/func -- shown in the drawer

        # --- fast set: everything that actually moves, every poll ---
        freq = _num(self._get1(sock, "f", raw))
        mode_lines = self._cmd(sock, "m", 2)
        raw["m"] = " / ".join(mode_lines) or "(no reply)"
        mode = mode_lines[0] if mode_lines and not mode_lines[0].startswith("RPRT") else None
        passband = _num(mode_lines[1]) if len(mode_lines) > 1 and not mode_lines[1].startswith("RPRT") else None
        vfo = self._get1(sock, "v", raw)
        split_lines = self._cmd(sock, "s", 2)
        raw["s"] = " / ".join(split_lines) or "(no reply)"
        split = (split_lines and split_lines[0] == "1")
        tx_freq = _num(self._get1(sock, "i", raw)) if split else None
        ptt = (self._get1(sock, "t", raw) == "1")
        strength = _num(self._get1(sock, "l STRENGTH", raw))

        swr = alc = comp = power_w = power_meter = power_set = None
        if ptt:  # TX meters -- only worth reading while transmitting
            power_set = _num(self._get1(sock, "l RFPOWER", raw))
            swr = _num(self._get1(sock, "l SWR", raw))
            # SWR is physically >= 1.0; anything BELOW is a garbage /
            # uninitialised read. A flat 1.000 right after key-up is
            # WFView's rigctld meter cache not yet refreshed -- it
            # settles to the real value a few seconds into a sustained
            # transmit, so it's shown as-is rather than suppressed.
            if swr is not None and swr < 1.0:
                swr = None
            alc = _num(self._get1(sock, "l ALC", raw))
            comp = _num(self._get1(sock, "l COMP_METER", raw))
            power_w = _num(self._get1(sock, "l RFPOWER_METER_WATTS", raw))
            power_meter = _num(self._get1(sock, "l RFPOWER_METER", raw))

        # --- slow set: PA temp + flags + antenna, refreshed every N sec
        #     and carried on every snapshot in between ---
        if now - self._slow_at >= config.RIG_PANEL_SLOW_EVERY_SEC:
            sraw = {}
            self._slow_cache = {
                "temp_frac": _num(self._get1(sock, "l TEMP_METER", sraw)),
                "funcs": {
                    "tuner": _flag(self._get1(sock, "u TUNER", sraw)),
                    "nb":    _flag(self._get1(sock, "u NB", sraw)),
                    "nr":    _flag(self._get1(sock, "u NR", sraw)),
                    "anf":   _flag(self._get1(sock, "u ANF", sraw)),
                    "comp":  _flag(self._get1(sock, "u COMP", sraw)),
                },
                "antenna": _num(self._get1(sock, "y", sraw)),
                "power_set": _num(self._get1(sock, "l RFPOWER", sraw)),
                "raw": sraw,
            }
            self._slow_at = now
        sc = self._slow_cache
        temp_frac = sc.get("temp_frac")
        # A finals-temp meter never truly reads 0.000 -- WFView's rigctld
        # answers `l TEMP_METER` but returns a flat 0 for a rig it doesn't
        # map, so treat that as "not reported" (card shows a dash, the
        # PA-temp alert can't fire on it) rather than a misleading "0%".
        if not temp_frac:
            temp_frac = None
        funcs = sc.get("funcs") or {"tuner": None, "nb": None, "nr": None, "anf": None, "comp": None}
        antenna = sc.get("antenna")
        if power_set is None:
            power_set = sc.get("power_set")  # slider position (RX fallback for the PWR bar frac)
        raw = {**sc.get("raw", {}), **raw}

        band = freq_to_band(int(freq)) if freq else ""
        if not band and freq and 5_250_000 <= freq <= 5_450_000:
            band = "60m"

        temp_c = None
        if temp_frac is not None and self._temp_cal:
            lo, hi = self._temp_cal
            temp_c = lo + (hi - lo) * temp_frac

        # Forward power in watts. RFPOWER_METER_WATTS is Hamlib >= 4.6 /
        # backend-specific (WFView's rigctld answers RPRT -1). Fall back
        # to RFPOWER_METER -- which Hamlib nominally specifies as 0..1,
        # but WFView's rigctld returns in *watts* (e.g. 4.52), so a value
        # above ~1.5 is taken as already-watts and only a true 0..1
        # fraction is scaled by the rig's rated watts.
        if not power_w:
            if power_meter is not None and power_meter > 1.5:
                power_w = power_meter
            elif power_meter:
                power_w = power_meter * config.RIG_RATED_WATTS
            else:
                power_w = None
        power_frac = None  # clean 0..1 for the PWR bar
        if power_w is not None:
            power_frac = min(1.0, power_w / config.RIG_RATED_WATTS) if config.RIG_RATED_WATTS else None
        elif power_set is not None:
            power_frac = power_set

        return {
            "reachable": True,
            "freq_hz": int(freq) if freq else None,
            "mode": mode,
            "passband_hz": int(passband) if passband else None,
            "band": band,
            "vfo": "B" if (vfo or "").upper().endswith("B") else "A",
            "split": bool(split),
            "tx_freq_hz": int(tx_freq) if tx_freq else None,
            "ptt": bool(ptt),
            "strength_db": strength,
            "power_set": power_set,
            "swr": swr,
            "alc": alc,
            "comp": comp,
            "power_w": round(power_w, 1) if power_w is not None else None,
            "power_frac": power_frac,   # clean 0..1 for the PWR bar
            "power_meter": power_meter,  # raw RFPOWER_METER reply (may be watts or 0..1 -- diagnostic only)
            "temp_frac": temp_frac,
            "temp_c": round(temp_c, 1) if temp_c is not None else None,
            "funcs": funcs,
            "antenna": (int(antenna) + 1) if antenna is not None else None,  # 1-indexed for display
            "raw": raw,
            "polled_at": now,
        }

    def _apply(self, snap):
        now = snap["polled_at"]
        with self._lock:
            self._snap = snap
            if snap.get("temp_frac") is not None:
                self._temps.append((now, snap["temp_frac"]))
            self._update_timeline(snap, now)
            self._check_pa(snap.get("temp_frac"), snap.get("temp_c"), now)

    # --- operating timeline (band/mode history) ---

    def _update_timeline(self, snap, now):
        band, mode = snap.get("band"), snap.get("mode")
        if not band or not mode:
            return
        if self._timeline and self._timeline[-1].get("end") is None \
                and self._timeline[-1]["band"] == band and self._timeline[-1]["mode"] == mode:
            return  # same segment continues
        self._close_open_segment(now)
        self._timeline.append({"band": band, "mode": mode, "start": now, "end": None})

    def _close_open_segment(self, now):
        if self._timeline and self._timeline[-1].get("end") is None:
            self._timeline[-1]["end"] = now

    # --- PA-temperature alert (Notifications source "rig_pa") ---

    def _check_pa(self, frac, temp_c, now):
        if frac is None:
            return
        thr = self._alert_frac
        if frac >= thr:
            if self._pa_since is None:
                self._pa_since = now
            elif not self._pa_high and (now - self._pa_since) >= config.RIG_PA_ALERT_SUSTAIN_SEC:
                self._pa_high = True
                self._emit_pa("high", frac, temp_c, now)
        else:
            self._pa_since = None
            if self._pa_high:
                self._pa_high = False
                self._emit_pa("normal", frac, temp_c, now)

    def _emit_pa(self, kind, frac, temp_c, now):
        if not self._alert_on:
            return
        payload = {"at": now, "kind": kind, "pct": round(frac * 100), "temp_c": temp_c}
        self._pa_events.append(payload)
        try:
            import storage_notifications
            storage_notifications.log_notification("rig_pa", now, payload, 50)
        except Exception:
            pass

    # --- rigctld line I/O ---

    @staticmethod
    def _cmd(sock, line, nlines=1):
        sock.sendall((line + "\n").encode())
        buf = b""
        got = []
        while len(got) < nlines:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and len(got) < nlines:
                ln, buf = buf.split(b"\n", 1)
                ln = ln.decode(errors="replace").strip()
                got.append(ln)
                if ln.startswith("RPRT"):
                    return got  # error / terminal -- don't wait for more lines
        return got

    @classmethod
    def _get1(cls, sock, line, raw=None):
        """Send a 1-line-reply command; return the reply string, or None
        on an error / empty reply. If `raw` is a dict, the exact reply
        (incl. an `RPRT -n` error) is recorded under `line` -- surfaced
        in the card's drawer so it's obvious what a given rig / Hamlib
        build actually answers for each level/func."""
        r = cls._cmd(sock, line, 1)
        val = r[0] if r else ""
        if raw is not None:
            raw[line] = val or "(no reply)"
        if not val or val.startswith("RPRT"):
            return None
        return val


def _close(sock):
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _flag(v):
    if v is None:
        return None
    return v == "1"
