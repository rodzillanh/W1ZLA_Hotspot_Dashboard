"""Live AX.25/APRS packet decode via wfweb's own built-in Dire Wolf-style
modem (v5.6) -- wfweb 0.9.1 exposes this over its own native WebSocket
(REST_API.md's "WebSocket messages -- Packet (Dire Wolf)" section), the
SAME wss://<host>:<port>/ endpoint wfweb_power.py/wfweb_memory.py already
talk to. Confirmed live (2026-09) against the user's real wfweb +
IC-7300MK2: sending {"cmd":"packetEnable","value":true} produced a real
{"type":"packetStatus","enabled":true,"mode":300} broadcast within a
couple of seconds, then {"cmd":"packetEnable","value":false} cleanly
turned it back off again.

Three demodulator modes, confirmed from wfweb's own REST_API.md table:
  300  -- HF AFSK (mark 1600 / space 1800 Hz, 200 Hz shift)
  1200 -- VHF AFSK Bell 202 (mark 1200 / space 2200 Hz), standard APRS
  9600 -- VHF G3RUH scrambled baseband FSK

IMPORTANT, not a guess but read directly from wfweb's own doc table:
this is ONE SHARED demodulator on wfweb's own server, not a
per-websocket-client instance -- enabling it (or changing its mode)
from this app also turns packet decoding on, or changes its mode, in
wfweb's own web UI for anyone else using that same wfweb instance at
the same time. This class only ever sends packetEnable=false on its
OWN enabled->disabled transition (this app's own Settings toggle
turned off), never on every reconnect/disable-by-default, so a
reconnect after a network blip doesn't fight a simultaneous wfweb web
UI user who separately turned decoding on themselves. There is no way
to know from this side whether someone else independently has it on
for their own reasons, so this app's own disable CAN turn off packet
decoding out from under them -- a real, disclosed limitation of a
single shared modem with no per-client ownership tracking, not solved
here.

RX-only, by wfweb's own design: REST_API.md's client->server command
table for this feature has only packetEnable/packetSetMode, no
packet-send/TX command at all in wfweb 0.9.1 -- so there's nothing for
this module to offer beyond monitoring what the modem decodes.

Same "persistent connection, reconfigure in place via a generation
counter, reconnect with backoff" shape as wfweb_power.py -- one
connection for the whole app (a Settings-level feature, not
per-hotspot), not openspot.py's per-device manager shape.
"""
import json
import ssl
import threading
import time
from collections import deque

import websocket

RECV_TIMEOUT = 30
RECONNECT_BACKOFF = 10
MAX_FRAMES = 100


class WfwebPacketClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._host = ""
        self._port = 8080
        self._use_ssl = True
        self._mode = 300
        self._enabled = False
        self._gen = 0
        self._ws = None
        self._connected = False
        self._modem_enabled = None  # server's own reported state, None until a packetStatus arrives
        self._modem_mode = None
        self._last_error = None
        self._frames = deque(maxlen=MAX_FRAMES)  # newest last

    def configure(self, enabled, host, port, use_ssl, mode=300):
        host = (host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8080
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            mode = 300
        if mode not in (300, 1200, 9600):
            mode = 300
        enabled = bool(enabled) and bool(host)
        use_ssl = bool(use_ssl)
        with self._lock:
            was_enabled = self._enabled
            new = (enabled, host, port, use_ssl, mode)
            if new == (self._enabled, self._host, self._port, self._use_ssl, self._mode):
                return  # no-op, cheap to call on every settings save
            self._enabled, self._host, self._port, self._use_ssl, self._mode = new
            self._gen += 1
            gen = self._gen
            ws = self._ws
            if not enabled:
                self._connected = False
                self._modem_enabled = None
                self._modem_mode = None
        if was_enabled and not enabled and ws is not None:
            # Turn the shared modem back off on OUR way out -- see module
            # docstring for why this only fires on this exact transition.
            try:
                ws.send(json.dumps({"cmd": "packetEnable", "value": False}))
            except Exception:
                pass
        if enabled:
            threading.Thread(target=self._loop, args=(gen,), daemon=True).start()

    def status(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "modem_enabled": self._modem_enabled,
                "mode": self._modem_mode if self._modem_mode is not None else self._mode,
                "error": self._last_error,
                "frames": list(self._frames),
            }

    # --- background loop, same shape as wfweb_power.py ---

    def _loop(self, gen):
        while True:
            with self._lock:
                if gen != self._gen:
                    return
                host, port, use_ssl, mode = self._host, self._port, self._use_ssl, self._mode
            try:
                self._run_once(gen, host, port, use_ssl, mode)
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

    def _run_once(self, gen, host, port, use_ssl, mode):
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
            ws.send(json.dumps({"cmd": "packetEnable", "value": True}))
            ws.send(json.dumps({"cmd": "packetSetMode", "value": mode}))
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
                    continue  # binary frame (spectrum/waterfall/audio), not relevant here
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                with self._lock:
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
        if mtype == "packetStatus":
            with self._lock:
                if "enabled" in msg:
                    self._modem_enabled = bool(msg["enabled"])
                if "mode" in msg:
                    self._modem_mode = msg["mode"]
        elif mtype == "packetRxFrame":
            frame = {
                "at": time.time(),  # our own receipt time -- same reasoning
                                     # as every other listener in this app:
                                     # a reliable local clock beats trusting
                                     # a remote-reported "ts" for "N ago" math
                "src": msg.get("src") or "",
                "dst": msg.get("dst") or "",
                "path": msg.get("path") or [],
                "info": msg.get("info") or "",
                "alevel": msg.get("alevel"),
            }
            with self._lock:
                self._frames.append(frame)
