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
        freq = _num(self._get1(sock, "f"))
        mode_lines = self._cmd(sock, "m", 2)
        mode = mode_lines[0] if mode_lines and not mode_lines[0].startswith("RPRT") else None
        passband = _num(mode_lines[1]) if len(mode_lines) > 1 and not mode_lines[1].startswith("RPRT") else None
        vfo = self._get1(sock, "v")
        split_lines = self._cmd(sock, "s", 2)
        split = (split_lines and split_lines[0] == "1")
        tx_freq = _num(self._get1(sock, "i")) if split else None
        ptt = (self._get1(sock, "t") == "1")

        strength = _num(self._get1(sock, "l STRENGTH"))
        power_set = _num(self._get1(sock, "l RFPOWER"))
        swr = _num(self._get1(sock, "l SWR"))
        alc = _num(self._get1(sock, "l ALC"))
        comp = _num(self._get1(sock, "l COMP_METER"))
        power_w = _num(self._get1(sock, "l RFPOWER_METER_WATTS"))
        power_meter = _num(self._get1(sock, "l RFPOWER_METER"))
        temp_frac = _num(self._get1(sock, "l TEMP_METER"))

        funcs = {
            "tuner": _flag(self._get1(sock, "u TUNER")),
            "nb":    _flag(self._get1(sock, "u NB")),
            "nr":    _flag(self._get1(sock, "u NR")),
            "anf":   _flag(self._get1(sock, "u ANF")),
            "comp":  _flag(self._get1(sock, "u COMP")),
        }
        antenna = _num(self._get1(sock, "y"))

        band = freq_to_band(int(freq)) if freq else ""
        if not band and freq and 5_250_000 <= freq <= 5_450_000:
            band = "60m"

        temp_c = None
        if temp_frac is not None and self._temp_cal:
            lo, hi = self._temp_cal
            temp_c = lo + (hi - lo) * temp_frac

        # RFPOWER_METER is 0..1; scale by the rig's rated watts as a
        # fallback when RFPOWER_METER_WATTS isn't supported.
        if power_w is None and power_meter is not None:
            power_w = power_meter * config.RIG_RATED_WATTS

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
            "temp_frac": temp_frac,
            "temp_c": round(temp_c, 1) if temp_c is not None else None,
            "funcs": funcs,
            "antenna": (int(antenna) + 1) if antenna is not None else None,  # 1-indexed for display
            "polled_at": time.time(),
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
    def _get1(cls, sock, line):
        r = cls._cmd(sock, line, 1)
        if not r or r[0].startswith("RPRT") or r[0] == "":
            return None
        return r[0]


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
