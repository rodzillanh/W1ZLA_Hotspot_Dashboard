"""WebSocket client for openSPOT 4 (SharkRF) hotspots.

Unlike WPSD/ASL3, openSPOT4 has no SSH access at all -- it's a closed
embedded device controlled entirely over HTTP + WebSocket. This whole
protocol was verified live against a real openSPOT 4 Pro (browser
dev-tools network capture), NOT from SharkRF's own published docs
(github.com/sharkrf/osp-http-api etc.), which describe an OLDER openSPOT
generation with different, incompatible endpoint names (e.g.
`checkauth.cgi` there vs. `/checktok` on this firmware -- confirmed by a
live 404 against the documented name, then finding the real one via
browser network capture). Same "verify against the real thing"
discipline as everywhere else in this project.

Confirmed live:
- Auth check: GET /checktok with `Authorization: Bearer <jwt>` -> 200.
- gettok/login (obtaining a fresh JWT) -- initially ported from
  SharkRF's older-gen docs as an unverified guess (the first capture
  session only ever saw an already-issued JWT), but a later fresh-page-
  load capture caught both by name, confirming the guessed paths exactly.
- Live status/call data streams over a WebSocket, not polling:
  ws://<ip>/<jwt> (the JWT goes in the URL path, not a header -- a
  workaround since browser JS can't set custom WS handshake headers),
  Sec-WebSocket-Protocol: openspot4. Confirmed via a captured 101
  Switching Protocols upgrade.
- Real JSON message shapes observed streaming over that socket (see
  _handle_message below for exact per-type handling).

Call lifecycle tracking is driven by "calllog" messages, not the
human-readable "log" text lines -- confirmed live against BOTH a DMR
call (Homebrew/BrandMeister connector) and a C4FM/YSF call (YSF
Reflector connector, "TGIF"), which turned out to have genuinely
different "log" line formats (DMR's has a "[N]" channel bracket; C4FM's
does not, and never emits an equivalent "call started" log line at
all -- only "calllog" reliably signals a call starting for every mode
tested so far). A "calllog" entry with duration 0.0 is a call starting;
the SAME "id" reappearing later with a real (>0) duration is that call
ending, with its own ber/loss/rssi -- this is the same shape for DMR and
C4FM, unlike the "log" lines which differ per mode. The "log" lines are
only still used to opportunistically learn the human mode name (DMR/
YSF) via each mode's own "<x>ct:" prefix -- see _MODE_BY_PREFIX. D-STAR/
NXDN/P25 have not been tested at all; their calllog "src" field shape
(DMR-ID-like vs. already-a-callsign, see _apply_call_start) and mode
prefix are both unverified guesses if they ever come up.

An openSPOT4's admin password is NOT one fixed device-wide credential --
each config profile can have its own separate password (confirmed live:
a working password started returning 401 immediately after switching
the device to a different profile, which reboots it into that profile).
See _describe_login_error() -- a 401 specifically calls this out rather
than a bare "Unauthorized", since it's the most likely cause after a
profile change, not a connection/network problem.
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import websocket

import config
from storage import load_hotspots

# --- HTTP auth endpoints ---

# Ported from SharkRF's older-gen openSPOT docs (github.com/sharkrf/
# osp-http-api) as a starting-point guess -- confirmed correct by a later
# live dev-tools capture of a real fresh page load, which caught both
# gettok and login by name (see module docstring). /checktok was
# confirmed live from the start (validating an *already-issued* JWT).
_GETTOK_PATH = "/gettok"
_LOGIN_PATH = "/login"
_CHECKTOK_PATH = "/checktok"

# Confirmed live via a captured 101 Switching Protocols WS upgrade.
_WS_SUBPROTOCOL = "openspot4"

# Matches just the mode-name prefix off the FRONT of any "log" line that
# has one -- e.g. "dmrct: [0] grp voice call started, ..." or
# "c4fmct: call ended, ...". Deliberately not trying to parse the rest of
# the line (dst/src/id/dur/ber/loss/rssi) -- calllog gives all of that in
# a consistent structured shape for every mode tested, this regex exists
# purely to opportunistically learn the human mode name for display.
_MODE_LOG_RE = re.compile(r"^(\w+ct):")

# Only "dmrct" (DMR) and "c4fmct" (C4FM/YSF) confirmed live. Other modes
# (D-STAR, NXDN, P25) presumably have their own "<x>ct:" prefix --
# unverified guess at the pattern, so anything not in this map falls back
# to stripping "ct" and upper-casing the prefix rather than silently
# dropping the mode entirely.
_MODE_BY_PREFIX = {
    "dmrct": "DMR",
    "c4fmct": "YSF",
}


def _mode_for_prefix(prefix: str) -> str:
    if prefix in _MODE_BY_PREFIX:
        return _MODE_BY_PREFIX[prefix]
    return prefix[:-2].upper() if prefix.endswith("ct") else prefix.upper()


def _login(ip: str, password: str) -> str:
    """Full HTTP auth handshake -> a fresh JWT. Endpoint names/shapes
    confirmed live against this firmware (see module docstring)."""
    req = urllib.request.Request(f"http://{ip}{_GETTOK_PATH}")
    with urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT) as resp:
        token = json.loads(resp.read())["token"]

    digest = hashlib.sha256((token + password).encode()).hexdigest()
    body = json.dumps({"token": token, "digest": digest}).encode()
    req = urllib.request.Request(
        f"http://{ip}{_LOGIN_PATH}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())["jwt"]


def _collect_passwords(hotspot: dict) -> list:
    """openSPOT4 config profiles can each have their own separate device
    password (confirmed live), and only one profile is ever active on a
    given physical device at a time. Rather than tracking which profile
    is currently active (there's no API for that -- see CLAUDE.md), just
    collect every password the user has told us about for this device --
    the primary "pass" field plus any extras stored for other profiles --
    and try them all at login time; whichever one matches the currently
    active profile succeeds. Blank/duplicate entries are dropped, order
    preserved (primary first) so the common single-profile case tries
    its one password first, not last."""
    candidates = [hotspot.get("pass", "")]
    extra = hotspot.get("openspot4_extra_pass", "") or ""
    candidates += [line.strip() for line in extra.splitlines()]
    seen = set()
    result = []
    for pw in candidates:
        if pw and pw not in seen:
            seen.add(pw)
            result.append(pw)
    return result


def _login_any(ip: str, passwords: list) -> "tuple[str, int]":
    """Tries each password in order, returning (jwt, index) for the first
    that succeeds. Raises the LAST encountered exception if every one
    fails -- if the device is genuinely unreachable every attempt fails
    the same way regardless of password, and if every attempt is a 401
    (all stored passwords rejected by whichever profile is currently
    active), the last one is just as representative as any other."""
    if not passwords:
        raise ValueError("no password configured")
    last_exc: Exception = ValueError("no password configured")
    for i, pw in enumerate(passwords):
        try:
            return _login(ip, pw), i
        except Exception as e:
            last_exc = e
    raise last_exc


def _describe_login_error(e: Exception, num_passwords: int = 1) -> str:
    """A 401 specifically means every stored password was rejected --
    worth calling out that openSPOT4 config profiles can each carry
    their OWN separate device password (confirmed live: switching
    profiles, which reboots the device into that profile, silently
    invalidated a password that had worked moments before against a
    different profile). Any other error (unreachable/timeout/etc.) is
    left as the plain exception text."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        if num_passwords > 1:
            return (
                f"Login failed: HTTP 401 Unauthorized -- none of the "
                f"{num_passwords} stored passwords were accepted for "
                f"whichever config profile is currently active"
            )
        return (
            "Login failed: HTTP 401 Unauthorized -- wrong password, or the "
            "device is on a different config profile than when this "
            "password was set (each openSPOT4 profile can have its own "
            "separate password -- add its password under \"Additional "
            "profile passwords\" so both are tried automatically)"
        )
    return f"Login failed: {e}"


def _check_token(ip: str, jwt: str) -> None:
    """Cheap pre-flight before the heavier WS handshake -- confirmed live:
    GET /checktok + Authorization: Bearer <jwt> -> 200 for a valid token.
    Raises on any non-200."""
    req = urllib.request.Request(
        f"http://{ip}{_CHECKTOK_PATH}", headers={"Authorization": f"Bearer {jwt}"}
    )
    urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT).close()


class _OpenSpot4Worker:
    """One persistent WebSocket connection to one openSPOT4 device.
    Unlike aprs_inbox.py's reconnect (same static passcode every time), a
    reconnect here must redo the full HTTP auth handshake (fresh token ->
    digest -> login -> new JWT) before reopening the WS, since a JWT
    can't just be reused indefinitely."""

    def __init__(self, hotspot: dict, monitor):
        self._ip = hotspot["ip"]
        self._passwords = _collect_passwords(hotspot)
        self._monitor = monitor
        self._stop_event = threading.Event()
        self._active_call: dict | None = None  # {"id","src","dst"} for the one in-flight call this device is tracking
        # calllog entries get rebroadcast periodically even after a call has
        # ended (observed live: the same id/duration pair re-appearing
        # ~10s later, unchanged) -- dedupe so a rebroadcast doesn't
        # re-trigger "call ended" processing (re-logging Fleet activity,
        # re-touching last_heard, etc.) every time it re-arrives.
        self._finalized_call_ids: deque = deque(maxlen=50)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def config_matches(self, hotspot: dict) -> bool:
        return (hotspot.get("ip") == self._ip
                and _collect_passwords(hotspot) == self._passwords)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception:
                self._monitor.mark_external_offline(self._ip)
            if not self._stop_event.is_set():
                self._stop_event.wait(config.OPENSPOT4_RECONNECT_BACKOFF)

    def _run_once(self) -> None:
        jwt, _ = _login_any(self._ip, self._passwords)
        _check_token(self._ip, jwt)  # fail fast with a clear auth error before the WS upgrade
        ws = websocket.create_connection(
            f"ws://{self._ip}/{jwt}",
            subprotocols=[_WS_SUBPROTOCOL],
            timeout=config.OPENSPOT4_RECV_TIMEOUT,
        )
        try:
            self._active_call = None
            while not self._stop_event.is_set():
                raw = ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                self._handle_message(msg)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # --- message dispatch ---

    def _handle_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "status":
            self._on_status(msg)
        elif mtype == "log":
            self._on_log(msg.get("log", ""))
        elif mtype == "csd":
            self._on_csd(msg)
        elif mtype == "calllog":
            self._on_calllog(msg)
        # "wifirssi", "netchk", "cptimeouts" are intentionally not
        # surfaced on the dashboard card in this first pass.

    def _on_status(self, msg: dict) -> None:
        status = msg.get("status") or {}
        updates = {}
        rssi_vals = status.get("rssi_values_dbm") or []
        ber_vals = status.get("ber_values") or []
        if rssi_vals:
            updates["rssi"] = f"{rssi_vals[-1]} dBm"
        if ber_vals:
            updates["ber"] = f"{ber_vals[-1]}%"
        # Even an empty heartbeat is proof of life -- always touch the
        # monitor so the failure counter resets / status stays Online.
        self._monitor.apply_external_update(self._ip, updates)

    def _on_log(self, text: str) -> None:
        """Only used to opportunistically learn the human mode name (DMR/
        YSF/...) from whichever mode's own "<x>ct:" log line happens to
        show up -- call lifecycle itself is driven by _on_calllog, not
        this. Deliberately doesn't try to parse anything past the prefix
        (dst/src/dur/ber/etc. all come from calllog instead, in a
        consistent shape across modes, unlike these log lines which
        differ per mode -- confirmed live, DMR's has a "[N]" channel
        bracket and an explicit "call started" line, C4FM's has neither)."""
        m = _MODE_LOG_RE.match(text)
        if m:
            self._monitor.apply_external_update(self._ip, {"mode": _mode_for_prefix(m.group(1))})

    def _on_calllog(self, msg: dict) -> None:
        """The universal call-lifecycle signal, confirmed live across two
        different modes/connectors (DMR/Homebrew and C4FM/YSF Reflector):
        a "calllog" entry with duration 0.0 is a call starting; the SAME
        "id" reappearing later with a real duration is that call ending.
        This is a structured JSON shape that's consistent across modes,
        unlike the "log" text lines this replaced for call tracking."""
        call_id = msg.get("id")
        if not call_id:
            return
        src = str(msg.get("src") or "")
        dst = str(msg.get("dst") or "")
        duration = msg.get("duration")
        is_final = isinstance(duration, (int, float)) and duration > 0

        if not is_final:
            if self._active_call and self._active_call["id"] == call_id:
                return  # already tracking this one -- a rebroadcast of the same "started" state
            self._active_call = {"id": call_id, "src": src, "dst": dst}
            self._apply_call_start(src, dst)
            return

        if call_id in self._finalized_call_ids:
            return  # already processed this call's ending -- just a periodic rebroadcast
        self._finalized_call_ids.append(call_id)

        ber = msg.get("ber")
        rssi = msg.get("rssi")
        updates = {"is_active": False, "tx_start": None, "last_heard": time.time()}
        if isinstance(ber, (int, float)) and ber >= 0:
            updates["ber"] = f"{ber}%"
        if isinstance(rssi, (int, float)):
            updates["rssi"] = f"{rssi} dBm"
        self._monitor.apply_external_update(self._ip, updates)
        self._monitor.log_activity(self._ip)
        if self._active_call and self._active_call["id"] == call_id:
            self._active_call = None

    def _apply_call_start(self, src: str, dst: str) -> None:
        is_fav, fav_label = self._monitor.apply_favorite_match(src)
        self._monitor.apply_external_update(self._ip, {
            "is_active": True,
            "active_call": src,  # DMR ID placeholder (csd resolves it) or already a real callsign
            "talkgroup": dst,
            "tx_start": time.time(),
            "last_heard": None,
            "is_favorite": is_fav,
            "favorite_label": fav_label,
        })
        if not src.isdigit():
            # Not a DMR ID -- src is already a real callsign (confirmed
            # live for C4FM/YSF, which addresses stations by callsign
            # directly rather than a numeric ID), so there's no separate
            # "csd" resolution coming the way DMR gets one. Enrich it
            # here directly instead, same QRZ/RadioID/APRS path _on_csd
            # uses for DMR.
            self._enrich_caller(src)

    def _on_csd(self, msg: dict) -> None:
        active = self._active_call
        if not active:
            return  # a csd with no call we're tracking -- ignore defensively
        if msg.get("id_type") != "dmr" or str(msg.get("id")) != active["src"]:
            return
        callsign = msg.get("callsign")
        if not callsign:
            return
        self._enrich_caller(callsign, msg.get("name"))

    def _enrich_caller(self, call: str, device_name: str = None) -> None:
        """Shared caller-info enrichment for both DMR (via _on_csd, keyed
        off a resolved callsign) and C4FM/YSF (via _apply_call_start,
        already a callsign with no separate resolution step). openSPOT's
        own DMR-ID lookup (or C4FM/YSF's own callsign addressing) only
        gives a callsign+name, not location/photo/position -- reuse the
        same QRZ/RadioID/APRS enrichment path WPSD/ASL3 use for that. The
        device's own name (when given, DMR only) still takes priority
        over QRZ/RadioID's (fresher, a per-call lookup done by the
        device itself)."""
        caller_info = self._monitor.lookup_caller_info(call)
        resolved_name = device_name or caller_info["name"]
        is_fav, fav_label = self._monitor.apply_favorite_match(call)

        prior = self._monitor.snapshot().get(self._ip, {})
        history = list(prior.get("history", []))
        updates = {
            "active_call": call,
            "caller_name": resolved_name,
            "caller_location": caller_info["location"],
            "caller_image": caller_info["image_url"],
            "caller_lat": caller_info["lat"],
            "caller_lon": caller_info["lon"],
            "caller_source": caller_info["source"],
            "is_favorite": is_fav,
            "favorite_label": fav_label,
        }
        if not any(h.get("call") == call for h in history):
            history.insert(0, {
                "call": call, "name": resolved_name, "location": caller_info["location"],
                "lat": caller_info["lat"], "lon": caller_info["lon"], "source": caller_info["source"],
            })
            updates["history"] = history[:config.MAX_HISTORY]
        self._monitor.apply_external_update(self._ip, updates)


class OpenSpot4Manager:
    """Reconciles hotspots.json's type=="openspot4" entries against live
    per-device worker threads. Same shape as camera_stream.py's
    CameraStreamManager (dict of workers + lock + explicit stop/remove) --
    aprs_inbox.py's generation-counter trick isn't needed here since
    removal is explicit/synchronous (worker.stop()), not "supersede a
    stale thread." Eagerly started (not lazy-on-first-viewer, unlike
    camera workers) since this is a push source for the dashboard, not a
    viewer-driven stream."""

    def __init__(self, monitor):
        self._monitor = monitor
        self._lock = threading.Lock()
        self._workers: dict[str, _OpenSpot4Worker] = {}

    def reconcile(self, hotspots: list) -> None:
        desired = {h["ip"]: h for h in hotspots if h.get("type") == "openspot4"}
        to_stop = []
        with self._lock:
            for ip, worker in list(self._workers.items()):
                hs = desired.get(ip)
                if hs is None or not worker.config_matches(hs):
                    to_stop.append(self._workers.pop(ip))
            for ip, hs in desired.items():
                if ip not in self._workers:
                    worker = _OpenSpot4Worker(hs, self._monitor)
                    worker.start()
                    self._workers[ip] = worker
        for worker in to_stop:  # outside the lock -- mirrors CameraStreamManager
            worker.stop()

    def remove(self, ip: str) -> None:
        with self._lock:
            worker = self._workers.pop(ip, None)
        if worker is not None:
            worker.stop()

    def run_forever(self) -> None:
        """Periodic safety-net reconcile (belt-and-suspenders, same
        rationale as monitor.py's prune_stale) -- every route that
        mutates hotspots.json already calls reconcile() explicitly, so
        this loop mostly no-ops in practice."""
        while True:
            self.reconcile(load_hotspots())
            time.sleep(config.POLL_INTERVAL)

    def test_connection(self, ip: str, password: str, extra_passwords: str = "") -> tuple[bool, str]:
        """Settings 'Test' button -- login + checktok only, no persistent
        WS opened (mirrors /api/test_asl_node's non-persistent smoke test).
        extra_passwords is the raw multi-line textarea value (other config
        profiles' passwords) -- tried in order alongside the primary one,
        same as the real worker does."""
        passwords = _collect_passwords({"pass": password, "openspot4_extra_pass": extra_passwords})
        try:
            jwt, idx = _login_any(ip, passwords)
        except Exception as e:
            return False, _describe_login_error(e, len(passwords))
        try:
            _check_token(ip, jwt)
        except Exception as e:
            return False, f"Token check (checktok) failed: {e}"
        which = "primary password" if idx == 0 else f"additional password #{idx}"
        return True, f"Login succeeded using the {which}"
