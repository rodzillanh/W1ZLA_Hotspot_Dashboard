"""Idle detection + tune/yield/restore logic for the SSTV card.

sstv_rx.py only listens; this module decides WHEN. When the operator's HF
rig has been left alone long enough, it tunes the rig to the SSTV calling
frequency (14.230 MHz USB by default) and tells the receiver to start
listening; the moment anyone touches the rig it gets out of the way.

What counts as "the rig is being used" (any of these resets the idle
clock, and the last two also make an active SSTV session yield):
  - the rig is transmitting (wfweb's `transmitting` flag, read by
    sstv_rx.py's own WebSocket -- reliable, unlike wfweb's rigctld bridge
    which was measured answering wrong frequency/mode values);
  - the rig's frequency or mode changed by anything other than this
    module (someone turned the knob, used wfweb's own web UI, WSJT-X...);
  - a dashboard interaction that drives the rig (app.py calls
    note_activity() from those routes -- tap-to-tune, Rig Panel actions,
    Quick Log);
  - the rig is unreachable or powered off (never tune a rig that's off).
"Radio isn't receiving any audio" from the original request is NOT used:
an HF receiver always emits band noise (measured: constant ~4400 RMS),
so audio level can't tell "idle" from "in use" -- transmit, retune and
dashboard activity are the real signals.

Receive-only, always: this module never keys the radio. The only rig
command it ever sends is a frequency/mode set through the same rigctld
path the POTA tap-to-tune already uses (passed in as `tune_fn`).

Yield vs. restore: if the operator retunes the rig while SSTV is active,
that IS their new choice -- SSTV just stops and never fights it (no
restore). "Restore my frequency" applies only when SSTV listening is
stopped by the operator from the card (or auto-listen is switched off
while active): then the pre-SSTV frequency/mode are put back.

State machine: "off" (card hidden) / "standby" (waiting for idle, or
paused because the rig is in use) / "active" (tuned and listening). The
active state is persisted (sstv/controller.json) so a dashboard restart
doesn't leave the rig parked on the SSTV frequency forgetting where it
came from.
"""
import json
import os
import threading
import time

import config

TUNE_GRACE_S = 10           # after a tune, ignore rig-state changes (the rig catches up)
FREQ_TOLERANCE_HZ = 300     # "still on the target" slack
FAILED_TUNE_COOLDOWN_S = 600
STOP_HOLD_MIN_S = 600       # after a manual Stop, don't auto-start again for at least this long


def _state_path():
    return os.path.join(config.CONFIG_DIR, "sstv", "controller.json")


class SstvController:
    def __init__(self, receiver, tune_fn, settings_fn, now_fn=time.time):
        self._rx = receiver
        self._tune = tune_fn                 # (freq_hz, mode) -> (ok, message)
        self._settings = settings_fn
        self._now = now_fn
        self._lock = threading.RLock()
        self.state = "off"
        self.reason = ""
        self._last_activity = now_fn()       # boot counts as activity: never tune the instant we start
        self._last_seen = None               # (freq_hz, mode) last observed on the rig
        self._grace_until = 0.0
        self._cooldown_until = 0.0
        self._prev = None                    # {"freq_hz", "mode"} before we tuned
        self._target = None
        self._active_since = None
        self._idle_min = 25
        self._note = ""                      # why we last stood down / failed -- kept on the card until the next start
        self._note_at = None
        self._resume = self._load_persisted()

    # ---------- persistence ----------

    @staticmethod
    def _load_persisted():
        try:
            with open(_state_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if d.get("active") else None
        except Exception:
            return None

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
            with open(_state_path(), "w", encoding="utf-8") as f:
                json.dump({"active": self.state == "active", "prev": self._prev, "target": self._target}, f)
        except Exception as e:
            print(f"[sstv] could not persist controller state: {e}")

    # ---------- helpers ----------

    def _cfg(self):
        s = self._settings() or {}
        try:
            idle = max(1, min(720, int(s.get("sstv_idle_minutes", 25))))
        except (TypeError, ValueError):
            idle = 25
        try:
            freq = int(s.get("sstv_freq_hz", 14230000))
        except (TypeError, ValueError):
            freq = 14230000
        return {
            "show": bool(s.get("show_sstv")),
            "auto": bool(s.get("sstv_auto_enabled")),
            "idle_s": idle * 60,
            "restore": bool(s.get("sstv_restore", True)),
            "freq_hz": freq,
            "mode": str(s.get("sstv_mode", "USB") or "USB").upper(),
        }

    def _on_target(self, freq_hz):
        return (self._target is not None and freq_hz is not None
                and abs(freq_hz - self._target["freq_hz"]) <= FREQ_TOLERANCE_HZ)

    # ---------- inputs ----------

    def note_activity(self, kind="other"):
        """app.py calls this from routes that drive/inspect the rig. A
        dashboard tune while SSTV is active means the operator chose
        somewhere else to be: yield immediately (no restore)."""
        with self._lock:
            now = self._now()
            self._last_activity = now
            if kind == "tune" and self.state == "active" and now >= self._grace_until:
                self._yield("You tuned the rig from the dashboard")

    def listen_now(self):
        """'Listen now' button: skip the idle wait. Returns (ok, message)."""
        with self._lock:
            cfg = self._cfg()
            if not cfg["show"]:
                return False, "The SSTV card is turned off"
            if self.state == "active":
                return True, "Already listening"
            return self._start(cfg, auto=False)

    def stop(self):
        """'Stop' from the card: stop listening and (if enabled) put the rig
        back where it was. Returns (ok, message)."""
        with self._lock:
            cfg = self._cfg()
            if self.state != "active":
                return True, "Not listening"
            msg = "Stopped"
            if cfg["restore"] and self._prev and self._prev.get("freq_hz"):
                ok, m = self._tune(self._prev["freq_hz"], self._prev.get("mode") or "")
                msg = "Stopped, rig restored" if ok else f"Stopped (couldn't restore the rig: {m})"
                self._grace_until = self._now() + TUNE_GRACE_S
            self._rx.set_listening(False)
            self.state = "standby"
            self._set_note("Stopped. Auto-listen resumes after the rig has been idle again")
            self.reason = self._note
            self._cooldown_until = self._now() + max(STOP_HOLD_MIN_S, cfg["idle_s"])
            self._prev = None
            self._target = None
            self._active_since = None
            self._persist()
            return True, msg

    # ---------- the loop body ----------

    def tick(self):
        """Called every couple of seconds by app.py's background loop."""
        with self._lock:
            try:
                self._tick()
            except Exception as e:
                print(f"[sstv] controller error: {type(e).__name__}: {e}")

    def _tick(self):
        cfg = self._cfg()
        self._idle_min = cfg["idle_s"] // 60
        now = self._now()
        if not cfg["show"]:
            if self.state == "active":
                self._stop_quietly(cfg)
            self.state, self.reason = "off", ""
            return
        st = self._rx.status()
        rig = st.get("rig") or {}
        key = (rig.get("freq_hz"), rig.get("mode"))
        if self.state == "off":
            self.state, self.reason = "standby", ""

        # a persisted active session from before a restart: resume if the rig is still parked there
        if self._resume is not None and st.get("connected") and key[0] is not None:
            resume, self._resume = self._resume, None
            self._target = resume.get("target")
            if self._target and self._on_target(key[0]):
                self._prev = resume.get("prev")
                self.state, self.reason = "active", ""
                self._active_since = now
                self._grace_until = now + TUNE_GRACE_S
                self._rx.set_listening(True)
                self._last_seen = key
                self._persist()
                return
            self._target = None

        if rig.get("transmitting"):
            self._last_activity = now
        if self._last_seen is not None and key[0] is not None and key != self._last_seen and now >= self._grace_until:
            self._last_activity = now
            if self.state == "active" and not (self._on_target(key[0]) and (key[1] or "").upper() == (self._target["mode"] or "").upper()):
                self._last_seen = key
                self._yield("You retuned the rig")
                return
        if key[0] is not None:
            self._last_seen = key

        if self.state == "active":
            if not st.get("connected"):
                self.reason = "Lost the connection to wfweb"
            elif rig.get("transmitting"):
                self._yield("The rig started transmitting")
            elif rig.get("power_state") is False:
                self._yield("The rig was powered off")
            elif now >= self._grace_until and key[0] is not None and not self._on_target(key[0]):
                # the rig never took the tune (or drifted off): don't sit "listening" somewhere else
                self._yield("The rig isn't on the SSTV frequency")
                self._cooldown_until = now + FAILED_TUNE_COOLDOWN_S
            return

        # standby
        if not cfg["auto"]:
            self.reason = "Auto-listen is off"
            return
        if not st.get("libs_ok", True):
            self.reason = "SSTV decoder libraries are not installed -- rebuild the container / re-run update.sh"
            return
        if not st.get("enabled"):
            self.reason = "wfweb host is not set (Settings → Integrations)"
            return
        if not st.get("connected"):
            self.reason = "Waiting for wfweb"
            return
        if rig.get("power_state") is False:
            self.reason = "The rig is powered off"
            return
        if key[0] is None:
            self.reason = "Waiting for the rig's frequency"
            return
        if now < self._cooldown_until:
            self.reason = self._note or "Waiting before trying again"
            return
        remaining = cfg["idle_s"] - (now - self._last_activity)
        if remaining > 0:
            self.reason = self._note or "Rig in use recently"
            return
        self._start(cfg, auto=True)

    # ---------- transitions (lock held) ----------

    def _start(self, cfg, auto):
        st = self._rx.status()
        rig = st.get("rig") or {}
        if not st.get("libs_ok", True):
            return False, st.get("error") or "SSTV decoder libraries are not installed"
        if not st.get("connected"):
            return False, "Not connected to wfweb"
        if rig.get("transmitting"):
            return False, "The rig is transmitting"
        if rig.get("power_state") is False:
            return False, "The rig is powered off"
        prev = {"freq_hz": rig.get("freq_hz"), "mode": rig.get("mode")}
        self._target = {"freq_hz": cfg["freq_hz"], "mode": cfg["mode"]}
        already = self._on_target(rig.get("freq_hz"))
        if not already:
            ok, msg = self._tune(cfg["freq_hz"], cfg["mode"])
            if not ok:
                self._target = None
                self._set_note(f"Couldn't tune the rig: {msg}")
                self.reason = self._note
                if auto:
                    self._cooldown_until = self._now() + FAILED_TUNE_COOLDOWN_S
                return False, self.reason
            self._prev = prev
        else:
            self._prev = self._prev or None       # already there: nothing to restore to
        now = self._now()
        self._grace_until = now + TUNE_GRACE_S
        self._active_since = now
        self.state, self.reason = "active", ""
        self._note = ""
        self._last_seen = (cfg["freq_hz"], cfg["mode"])
        self._rx.set_listening(True)
        self._persist()
        return True, "Listening"

    def _set_note(self, text):
        self._note, self._note_at = text, self._now()

    def _yield(self, reason):
        self._rx.set_listening(False)
        self._set_note(reason)
        self.state, self.reason = "standby", reason
        self._last_activity = self._now()
        self._prev = None
        self._target = None
        self._active_since = None
        self._persist()

    def _stop_quietly(self, cfg):
        if cfg["restore"] and self._prev and self._prev.get("freq_hz"):
            self._tune(self._prev["freq_hz"], self._prev.get("mode") or "")
        self._rx.set_listening(False)
        self._prev = self._target = self._active_since = None
        self._persist()

    # ---------- status ----------

    def status(self):
        with self._lock:
            cfg = self._cfg()
            now = self._now()
            remaining = None
            if self.state == "standby" and cfg["auto"]:
                remaining = max(0, int(cfg["idle_s"] - (now - self._last_activity)))
                if now < self._cooldown_until:
                    remaining = max(remaining, int(self._cooldown_until - now))
            return {
                "state": self.state, "reason": self.reason, "reason_at": self._note_at if self.reason == self._note else None,
                "idle_remaining_s": remaining, "idle_minutes": cfg["idle_s"] // 60,
                "auto": cfg["auto"], "restore": cfg["restore"],
                "target": {"freq_hz": cfg["freq_hz"], "mode": cfg["mode"]},
                "prev": self._prev, "active_since": self._active_since,
            }
