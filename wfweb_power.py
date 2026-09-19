"""Persistent WebSocket connection to wfweb's own native protocol (NOT
Hamlib rigctld -- see rig_panel.py for that, a completely separate
connection/port/protocol to the same physical rig) for the Rig Panel
card's Power On/Off + LAN Disconnect/Reconnect controls (v4.86).

Confirmed live by reading wfweb's own source directly
(github.com/adecarolis/wfweb, webserver.cpp/servermain.cpp) -- its own
REST_API.md claims "All radio control operations available via
WebSocket are also available via REST", which is FALSE for these three
actions: none of setPower/disconnectLan/reconnectLan appear anywhere in
the documented REST API, only in the WebSocket command switch
(webServer::onWsTextMessage()).

Wire protocol, confirmed from source, not guessed:
  - When wfweb runs with SSL enabled (the common case -- a self-signed
    cert, `wss://`), the WebSocket server shares the SAME port as the
    HTTPS web UI: webServer::onHttpConnection() peeks every incoming
    connection's HTTP headers for an `Upgrade: websocket` line and hands
    the raw socket to wsServer->handleConnection() -- there is a
    SEPARATE plain-HTTP-REST-only port too, but it does NOT also speak
    WebSocket. The peek only inspects headers, not the request path, so
    a bare `wss://<host>:<port>/` connects fine.
  - On connect, the server proactively pushes state with no request
    needed at all: onWsNewConnection() -> sendCurrentState() sends a
    `{"type":"rigInfo",...}` message immediately followed by a
    `{"type":"status", ...}` one (buildStatusJson()) once a rig is
    known. `{"cmd":"getStatus"}` also exists to re-request later, but
    isn't required for this module's own purposes.
  - Live status keeps arriving as `{"type":"status", ...,
    "powerState": bool, "lanConnected": bool, ...}` (periodic/on-change)
    and, on a LAN-state change specifically, a smaller
    `{"type":"lanStatus","isLan":bool,"lanConnected":bool}` broadcast
    (webServer::setLanInfo()).
  - Commands sent client->server: `{"cmd":"setPower","value":true|false}`,
    `{"cmd":"disconnectLan"}`, `{"cmd":"reconnectLan"}` -- confirmed
    directly from the command switch in webserver.cpp and
    servermain.cpp's real disconnectLan()/reconnectLan() (a genuine
    0x05 Icom LAN-session close + an in-place openRig() re-open, no
    container restart involved on wfweb's side either way).
  - `powerState`/`lanConnected` are independent booleans -- confirmed at
    several points in buildStatusJson()/setLanInfo() -- which is exactly
    why remote Power On can work at all: the browser<->wfweb WebSocket
    and wfweb<->radio LAN session are two separate things, and the
    LATTER can stay open even while the radio itself is powered off.
  - One documented caveat NOT modeled here since there's no way to know
    the configured rig model from this connection alone:
    powerOnNeedsRemoteJack() is true only for `modelName == "IC-7600"`
    (that model's network interface stays unpowered in standby unless
    wired through its [REMOTE] jack, not pure LAN).

Same "reconfigure in place via a generation counter, reconnect with
backoff" shape as hamalert.py/rbn.py -- deliberately NOT openspot.py's
per-device manager shape, since (like rig_panel.py's rigctld poller)
there is only ever one rig / one Settings section for this, not a list
of hotspots. Self-signed TLS on wfweb's own web UI means certificate
verification has to be disabled for `wss://` -- same trusted-LAN
assumption as every other rig/hotspot integration in this app (rigctld
itself has no auth either).
"""
import json
import ssl
import threading
import time

import websocket

RECV_TIMEOUT = 30
RECONNECT_BACKOFF = 10


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class WfwebPowerClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._host = ""
        self._port = 8080
        self._use_ssl = True
        self._enabled = False
        self._gen = 0
        self._ws = None
        self._connected = False
        self._power_state = None   # True/False/None (unknown -- no status seen yet)
        self._lan_connected = None
        self._freq_hz = None
        self._mode = None
        self._vfo = None
        self._last_error = None

    # --- config ---

    def configure(self, enabled, host, port, use_ssl):
        host = (host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8080
        enabled = bool(enabled) and bool(host)
        use_ssl = bool(use_ssl)
        with self._lock:
            new = (enabled, host, port, use_ssl)
            if new == (self._enabled, self._host, self._port, self._use_ssl):
                return  # no-op, cheap to call on every settings save
            self._enabled, self._host, self._port, self._use_ssl = new
            self._gen += 1
            gen = self._gen
            if not enabled:
                self._connected = False
                self._power_state = None
                self._lan_connected = None
                self._freq_hz = None
                self._mode = None
                self._vfo = None
        if enabled:
            threading.Thread(target=self._loop, args=(gen,), daemon=True).start()

    # --- public read/write ---

    def status(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "power_state": self._power_state,
                "lan_connected": self._lan_connected,
                # Frequency/mode/VFO read straight from wfweb's own rig
                # cache over its native protocol -- confirmed live
                # (2026-09) to be reliable on a real wfweb + IC-7300MK2
                # setup where wfweb's OWN rigctld compatibility bridge
                # (rig_panel.py's connection) answered a flatly wrong
                # literal "0.000000"/"UNKNOWN"/"None" for these same
                # three fields. app.py's /api/rig_panel merges these in
                # as an override on top of the rigctld snapshot.
                "freq_hz": self._freq_hz,
                "mode": self._mode,
                "vfo": self._vfo,
                "error": self._last_error,
            }

    def set_power(self, on):
        return self._send({"cmd": "setPower", "value": bool(on)})

    def disconnect_lan(self):
        return self._send({"cmd": "disconnectLan"})

    def reconnect_lan(self):
        return self._send({"cmd": "reconnectLan"})

    def _send(self, obj):
        with self._lock:
            ws = self._ws
            connected = self._connected
        if not connected or ws is None:
            return False, "Not connected to wfweb"
        try:
            ws.send(json.dumps(obj))
            return True, None
        except Exception as e:
            return False, str(e)

    @staticmethod
    def test_connection(host, port, use_ssl, timeout=8.0):
        """Settings 'Test connection' button -- a fresh, one-off
        connection, no persistent listener touched. The server pushes
        rigInfo/status immediately on connect (confirmed from source,
        see module docstring), so a single successful recv() of valid
        JSON is real evidence this is genuinely wfweb, not just some
        other TLS/WebSocket endpoint answering."""
        host = (host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8080
        if not host:
            return False, "Host required"
        scheme = "wss" if use_ssl else "ws"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if use_ssl else None
        try:
            ws = websocket.create_connection(
                f"{scheme}://{host}:{port}/", timeout=timeout, sslopt=sslopt,
            )
        except Exception as e:
            return False, f"Could not connect: {e}"
        try:
            raw = ws.recv()
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") in ("rigInfo", "status"):
                return True, "Connected to wfweb"
            return True, "Connected -- unexpected first message, but the WebSocket handshake succeeded"
        except Exception as e:
            return False, f"Connected but no valid response: {e}"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # --- background loop ---

    def _loop(self, gen):
        while True:
            with self._lock:
                if gen != self._gen:
                    return
                host, port, use_ssl = self._host, self._port, self._use_ssl
            try:
                self._run_once(gen, host, port, use_ssl)
            except Exception as e:
                with self._lock:
                    if gen != self._gen:
                        return
                    self._connected = False
                    self._last_error = str(e)
            with self._lock:
                if gen != self._gen:
                    return
            time.sleep(RECONNECT_BACKOFF)

    def _run_once(self, gen, host, port, use_ssl):
        scheme = "wss" if use_ssl else "ws"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if use_ssl else None
        ws = websocket.create_connection(
            f"{scheme}://{host}:{port}/", timeout=RECV_TIMEOUT, sslopt=sslopt,
        )
        try:
            with self._lock:
                if gen != self._gen:
                    return
                self._ws = ws
                self._connected = True
                self._last_error = None
            while True:
                with self._lock:
                    if gen != self._gen:
                        return
                raw = ws.recv()
                if raw is None or raw == "":
                    with self._lock:
                        self._last_error = "Connection closed"
                    return
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                with self._lock:
                    # A reconfigure (disable, or point at a different
                    # host) can land while this thread was blocked in
                    # recv() above -- re-check right before applying the
                    # message so a stale reply can't resurrect state
                    # configure() just reset for the new generation.
                    if gen != self._gen:
                        return
                self._handle_message(msg)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None
                self._connected = False
            try:
                ws.close()
            except Exception:
                pass

    def _handle_message(self, msg):
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "status":
            with self._lock:
                if "powerState" in msg:
                    self._power_state = bool(msg["powerState"])
                if "lanConnected" in msg:
                    self._lan_connected = bool(msg["lanConnected"])
                # `frequency`/`mode`/`selectedVfo` are confirmed from
                # wfweb's own source (buildStatusJson()) to be a real,
                # authoritative live snapshot each time -- a JSON null
                # is wfweb's own explicit "no value right now" signal
                # (its own code comment: distinguishing that from a
                # real 0 Hz), not a gap to hold the last value through,
                # so every "status" message updates these three fields
                # outright rather than only on a truthy value.
                if "frequency" in msg:
                    self._freq_hz = _as_int(msg["frequency"])
                if "mode" in msg:
                    self._mode = msg["mode"] if isinstance(msg["mode"], str) else None
                if "selectedVfo" in msg:
                    self._vfo = msg["selectedVfo"] if isinstance(msg["selectedVfo"], str) else None
        elif mtype == "lanStatus":
            with self._lock:
                if "lanConnected" in msg:
                    self._lan_connected = bool(msg["lanConnected"])
