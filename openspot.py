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

Only DMR call parsing ("dmrct:" log prefix) has been verified against a
real call. D-STAR/C4FM(YSF)/NXDN/P25 call start/end log line formats are
unverified and may use a different prefix -- see _MODE_BY_PREFIX.

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

# "dmrct: [0] grp voice call started, dst: 313136 src: 3120038 id: 9efed7317c1b13ce"
_CALL_START_RE = re.compile(
    r"^(\w+ct): \[(\d+)\] grp voice call started, dst: (\d+) src: (\d+) id: ([0-9a-f]+)$"
)
# "dmrct: [0] call ended, dur 28.9s ber 0.3%25 loss 0.0%25 rssi -47"
# The literal "%25" is a confirmed real firmware quirk (not a decoding
# bug on our end) -- parsed as-is, not "fixed" to "%". No id/src/dst in
# this line -- it's "whichever call this device is currently tracking"
# (channel index [0]), not re-matched by call id.
_CALL_END_RE = re.compile(
    r"^(\w+ct): \[(\d+)\] call ended, dur ([\d.]+)s ber ([\d.]+)%25 loss ([\d.]+)%25 rssi (-?\d+)$"
)

# Only "dmrct" (DMR) confirmed live against a real call. Other modes
# (C4FM/YSF, D-STAR, NXDN, P25) presumably have their own "<x>ct:"
# prefix -- unverified guess at the pattern, so anything not in this map
# falls back to stripping "ct" and upper-casing the prefix rather than
# silently dropping the mode entirely.
_MODE_BY_PREFIX = {
    "dmrct": "DMR",
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


def _describe_login_error(e: Exception) -> str:
    """A 401 specifically means the password was rejected -- worth calling
    out that openSPOT4 config profiles can each carry their OWN separate
    device password (confirmed live: switching profiles, which reboots
    the device into that profile, silently invalidated a password that
    had worked moments before against a different profile). Any other
    error (unreachable/timeout/etc.) is left as the plain exception text."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        return (
            "Login failed: HTTP 401 Unauthorized -- wrong password, or the "
            "device is on a different config profile than when this "
            "password was set (each openSPOT4 profile can have its own "
            "separate password)"
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
        self._password = hotspot.get("pass", "")
        self._monitor = monitor
        self._stop_event = threading.Event()
        self._active_call: dict | None = None  # the one in-flight call this device is tracking
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def config_matches(self, hotspot: dict) -> bool:
        return hotspot.get("ip") == self._ip and hotspot.get("pass", "") == self._password

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception:
                self._monitor.mark_external_offline(self._ip)
            if not self._stop_event.is_set():
                self._stop_event.wait(config.OPENSPOT4_RECONNECT_BACKOFF)

    def _run_once(self) -> None:
        jwt = _login(self._ip, self._password)
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
        # "wifirssi", "netchk", "cptimeouts", "calllog" are intentionally
        # not surfaced on the dashboard card in this first pass -- see
        # plan's non-goals. calllog is redundant with the "log" call-
        # start line for dst/src, and its own final duration/ber/loss/
        # rssi at call end are unverified as ever actually being sent.

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
        m = _CALL_START_RE.match(text)
        if m:
            prefix, channel, dst, src, call_id = m.groups()
            self._active_call = {
                "prefix": prefix,
                "channel": channel,
                "dst": dst,
                "src": src,
                "id": call_id,
            }
            is_fav, fav_label = self._monitor.apply_favorite_match(src)
            self._monitor.apply_external_update(self._ip, {
                "is_active": True,
                "active_call": src,  # placeholder DMR ID until a matching csd resolves a callsign
                "talkgroup": dst,
                "mode": _mode_for_prefix(prefix),
                "tx_start": time.time(),
                "last_heard": None,
                "is_favorite": is_fav,
                "favorite_label": fav_label,
            })
            return

        m = _CALL_END_RE.match(text)
        if m:
            prefix, channel, dur, ber, loss, rssi = m.groups()
            active = self._active_call
            # Must match the SAME call this device is tracking (prefix +
            # channel), not just "some call ended" -- this also rejects
            # the differently-worded "homebrew: ignoring call from N
            # ended" distractor line automatically, since that text never
            # matches this regex's "<mode>ct: [N] call ended, ..." shape
            # at all.
            if active and active["prefix"] == prefix and active["channel"] == channel:
                self._monitor.apply_external_update(self._ip, {
                    "is_active": False,
                    "tx_start": None,
                    "last_heard": time.time(),
                    "ber": f"{ber}%",
                    "rssi": f"{rssi} dBm",
                })
                self._monitor.log_activity(self._ip)
                self._active_call = None
            return
        # Anything else (net-chk, homebrew ping/pong, cptimeouts-adjacent
        # log text, etc.) is deliberately unmatched -- no explicit
        # exclusion list needed, same convention as digipi.py's PACKET_RE.

    def _on_csd(self, msg: dict) -> None:
        active = self._active_call
        if not active:
            return  # a csd with no call we're tracking -- ignore defensively
        if msg.get("id_type") != "dmr" or str(msg.get("id")) != active["src"]:
            return
        callsign = msg.get("callsign")
        device_name = msg.get("name")
        call = callsign or active["src"]

        # openSPOT's own DMR-ID lookup only gives callsign+name, not
        # location/photo/position -- reuse the same QRZ/RadioID/APRS
        # enrichment path WPSD/ASL3 use for that. Its live-resolved
        # name/callsign still take priority over QRZ/RadioID's (fresher,
        # a per-call lookup done by the device itself).
        if callsign:
            caller_info = self._monitor.lookup_caller_info(call)
        else:
            caller_info = {"name": None, "location": None, "image_url": None,
                            "lat": None, "lon": None, "source": None}
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

    def test_connection(self, ip: str, password: str) -> tuple[bool, str]:
        """Settings 'Test' button -- login + checktok only, no persistent
        WS opened (mirrors /api/test_asl_node's non-persistent smoke test)."""
        try:
            jwt = _login(ip, password)
        except Exception as e:
            return False, _describe_login_error(e)
        try:
            _check_token(ip, jwt)
        except Exception as e:
            return False, f"Token check (checktok) failed: {e}"
        return True, "Login and token check succeeded"
