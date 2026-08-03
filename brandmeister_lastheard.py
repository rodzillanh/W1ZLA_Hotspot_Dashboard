"""Persistent WebSocket connection to Brandmeister's own "Last Heard"
Socket.IO feed, for the Notifications card's optional "Brandmeister
favorite activity" alerts -- fires when a favorite callsign
(favorites.json) keys up ANYWHERE on the Brandmeister network, not just
through this fleet's own hotspots (that narrower case is already covered
by the existing APRS favorite-alert feature and monitor.py's own
favorite matching, both scoped to this fleet's own traffic only).

brandmeister.py's REST API has no equivalent of this -- confirmed via
Brandmeister's own OpenAPI spec (api.brandmeister.network/api-docs),
real-time activity only exists over a persistent connection, same
conclusion CLAUDE.md already records from investigating a "network-wide
top 5" card idea. This module was built entirely from live
reverse-engineering (same "verify against the real thing" discipline as
openspot.py/digipi.py), not any of Brandmeister's own docs, which don't
cover this at all:

- The actual host/path (wss://ws.brandmeister.network/lh/socket.io) was
  found by fetching brandmeister.network's own bundled frontend JS
  (assets/index-*.js) and locating where it calls socket.io-client with
  a `fb.lh.host`/`fb.lh.path` config object -- those exact values are
  injected at build time and aren't literal strings anywhere in the
  bundle, so the host was found by probing candidate subdomains
  directly against the standard Engine.IO polling handshake path
  (`/socket.io/?EIO=4&transport=polling`) until one returned a real
  Engine.IO open packet instead of a 404/503 wall.
  `ws.brandmeister.network` with path `/lh/socket.io` is the one that
  worked, confirmed with a real handshake response
  (`0{"sid":"...","upgrades":[...],"pingInterval":25000,...}`).
- This hand-rolls the Engine.IO v4 / Socket.IO v4 wire protocol over
  plain `websocket-client` (already a project dependency, used by
  openspot.py) rather than adding a new `python-socketio` pip dependency
  -- same reasoning as openspot.py's own custom framing over a full
  protocol library. Handshake: server sends `0{"sid":...}` (Engine.IO
  OPEN), client replies `40` (Socket.IO CONNECT to the default
  namespace), server confirms with `40{"sid":...}`. Engine.IO PING
  (`2`) needs a bare PONG (`3`) reply or the server drops the
  connection after `pingTimeout`.
- After connecting, the client must emit a `join` event with room name
  `"everything"` (Socket.IO EVENT: `42["join","everything"]`) to
  subscribe to the GLOBAL last-heard firehose -- confirmed live that
  nothing streams at all without this. The real frontend's own
  per-repeater/per-talkgroup views join a different, narrower room; only
  its global homepage ticker widget uses "everything".
- Live data then arrives as `42["mqtt",{"topic":"LH","payload":"<JSON
  string>"}]` events -- Brandmeister's own MQTT broker feed relayed
  verbatim through Socket.IO (hence the event name `mqtt` and the
  topic/payload envelope, even though this app never touches MQTT
  directly). `json.loads(payload)` gives the real fields, confirmed
  against several live events during this research: SourceCall/
  SourceName/SourceID, DestinationID/DestinationCall/DestinationName,
  LinkType/LinkTypeName/LinkCall/LinkName, ContextID, SessionID,
  SessionType, Slot, Route, State, Start/Stop (epoch seconds, Stop==0
  while a call is still in progress), RSSI, BER, ReflectorID, CallTypes
  (list, e.g. ["Group"]/["Private"]).
- SessionID dedupes repeated publishes for the same call (confirmed live
  that a call keeps re-publishing while active) -- a favorite alert
  fires only once per SessionID, not once per message.
"""
import json
import threading
import time
from collections import deque

import websocket  # websocket-client, already a project dependency (see openspot.py)

from storage import favorites_set
import storage_notifications

WS_URL = "wss://ws.brandmeister.network/lh/socket.io/?EIO=4&transport=websocket"
RECV_TIMEOUT = 40  # comfortably above the handshake's own ~20-25s pingInterval
RECONNECT_BACKOFF = 10
MAX_EVENTS = 50
SEEN_SESSIONS_MAX = 1000  # bounded SessionID dedupe cache -- oldest just falls off


class BrandmeisterLastHeardListener:
    """Generation-counter reconfigure-in-place, same shape as
    aprs_inbox.py's AprsInbox/wsjtx.py's WsjtxListener -- a listening
    socket needs its own thread lifecycle, and the enabled setting can
    change at any time via Settings -> Cards."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._enabled = False
        self._connected = False
        self._last_error: "str | None" = None
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._seen_sessions: deque = deque(maxlen=SEEN_SESSIONS_MAX)
        # Reseed from disk so a restart doesn't lose event history --
        # appends oldest-to-newest (see _handle_event), so replaying the
        # oldest-first persisted list through the same .append()
        # reproduces live insertion order.
        for payload in storage_notifications.recent("brandmeister", MAX_EVENTS):
            self._events.append(payload)

    def configure(self, enabled: bool) -> None:
        with self._lock:
            if enabled == self._enabled:
                return  # no-op, cheap to call on every settings save
            self._enabled = enabled
            self._generation += 1
            generation = self._generation
        if enabled:
            threading.Thread(target=self._run, args=(generation,), daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self._enabled, "connected": self._connected, "last_error": self._last_error}

    def events(self) -> list:
        with self._lock:
            return list(reversed(self._events))

    def _run(self, generation: int) -> None:
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                self._connect_once(generation)
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
            with self._lock:
                if generation != self._generation:
                    return
            time.sleep(RECONNECT_BACKOFF)

    def _connect_once(self, generation: int) -> None:
        ws = websocket.create_connection(
            WS_URL, timeout=RECV_TIMEOUT,
            header=["User-Agent: hotspot-dashboard/1.0"],
        )
        try:
            joined = False
            while True:
                with self._lock:
                    if generation != self._generation:
                        return
                msg = ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="ignore")
                if not isinstance(msg, str):
                    continue
                if msg.startswith("0{"):
                    ws.send("40")
                elif msg.startswith("40{") and not joined:
                    joined = True
                    with self._lock:
                        self._connected = True
                        self._last_error = None
                    ws.send('42["join","everything"]')
                elif msg == "2":
                    ws.send("3")
                elif msg.startswith("42"):
                    self._handle_event(msg)
        finally:
            with self._lock:
                self._connected = False
            try:
                ws.close()
            except Exception:
                pass

    def _handle_event(self, msg: str) -> None:
        """msg looks like 42["mqtt",{"topic":"LH","payload":"<json>"}] --
        never raises, so one malformed event never takes the listener
        thread down (same degrade-gracefully contract as every other
        integration here)."""
        try:
            packet = json.loads(msg[2:])  # strip the "42" Socket.IO EVENT type prefix
            if not isinstance(packet, list) or len(packet) < 2 or packet[0] != "mqtt":
                return
            envelope = packet[1]
            data = json.loads(envelope.get("payload", "{}"))
            source_call = (data.get("SourceCall") or "").strip().upper()
            if not source_call:
                return
            with self._lock:
                if source_call not in favorites_set():
                    return
                session_id = data.get("SessionID")
                if session_id is not None:
                    if session_id in self._seen_sessions:
                        return
                    self._seen_sessions.append(session_id)
                event = {
                    "callsign": source_call,
                    "name": data.get("SourceName") or None,
                    "destination_id": data.get("DestinationID"),
                    "destination_name": data.get("DestinationName") or None,
                    "link_type_name": data.get("LinkTypeName") or None,
                    "at": time.time(),
                }
                self._events.append(event)
        except Exception:
            return
        storage_notifications.log_notification("brandmeister", event["at"], event, MAX_EVENTS)
