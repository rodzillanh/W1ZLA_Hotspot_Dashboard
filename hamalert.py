"""Persistent Telnet connection to HamAlert's cluster-emulation interface
(hamalert.org:7300) for the optional "HamAlert" notification card -- same
"one persistent connection, reconnect with backoff" shape as
aprs_inbox.py, adapted for a plain TCP/Telnet stream instead of APRS-IS.

HamAlert (hamalert.org) lets a user define "triggers" on their own
account (DXCC needed, specific callsign, band/mode, etc.) against DX
cluster + Reverse Beacon Network + POTA/SOTA/PSK Reporter spots, then
push matching spots out over one of several "destinations" -- this
module uses the Telnet destination specifically, not the URL GET/POST
webhook one, since a webhook would require this dashboard to accept a
public inbound connection (a real, meaningfully bigger ask than every
other integration here, which all either poll outward or listen only on
the local network).

HamAlert has no official protocol docs for the Telnet interface (its own
maintainer confirmed on the support forum, forum.hamalert.org/t/
documentation-for-telnet-interface/682, that "there are no supported
commands other than those already documented" -- sh/dx N and set/json --
plus one undocumented `echo foo` command that echoes `foo` back, useful
here as a keepalive/liveness check since there's no other heartbeat).
The login handshake itself (send username, then password, no need to
wait for or match specific prompt text) was confirmed against a real,
working third-party integration -- WhiskeyTangoHotel's Pimoroni Galactic
Unicorn project (whiskeytangohotel.com/2023/05/hamalertorg-integration-
with-pimoroni.html) -- rather than guessed from classic packet-cluster
conventions alone. `set/json` switches the session to stream one JSON
object per spot line instead of classic DX-cluster plain text; confirmed
field shape (from that same source plus HamAlert forum discussion):
callsign, fullCallsign, band, mode, frequency, spotter, source (dxcluster/
rbn/pota/sota/wwff/pskreporter), dxcc, entity, comment, triggerComment.

NOT live-tested against a real HamAlert account (none available in this
dev environment) -- if this ever fails to connect or stops receiving
alerts, re-verify the login sequence and JSON line shape against a live
account/packet capture before assuming this parser is still correct,
same "verify against the real thing" discipline as every other
reverse-engineered protocol in this project (openspot.py, digipi.py).
"""
import json
import socket
import threading
import time
from collections import deque

HAMALERT_HOST = "hamalert.org"
HAMALERT_PORT = 7300
MAX_ALERTS = 50          # ring buffer size -- same magnitude as aprs_inbox.py's own recent-messages cap
RECV_TIMEOUT = 60        # generous -- a quiet stretch between trigger matches is normal, not a dead connection
HEARTBEAT_INTERVAL = 20  # how often to send `echo` to confirm the socket is still alive
RECONNECT_BACKOFF = 15


class HamAlertListener:
    """Reconfigured in place via a generation counter -- same pattern as
    aprs_inbox.py's AprsInbox and wsjtx.py's WsjtxListener. A stale
    thread notices the generation changed (checked before/after every
    blocking recv) and exits cleanly instead of running forever under
    stale credentials."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._enabled = False
        self._username = ""
        self._password = ""
        self._alerts = deque(maxlen=MAX_ALERTS)
        self._connected = False
        self._last_alert_at = None

    def configure(self, enabled: bool, username: str, password: str) -> None:
        username = (username or "").strip()
        password = password or ""
        with self._lock:
            if enabled == self._enabled and username == self._username and password == self._password:
                return  # no-op, cheap to call on every settings save
            self._enabled = enabled
            self._username = username
            self._password = password
            self._generation += 1
            generation = self._generation
            if not enabled:
                self._connected = False
        if enabled and username and password:
            threading.Thread(target=self._run, args=(generation, username, password), daemon=True).start()

    def recent(self) -> list:
        with self._lock:
            return list(self._alerts)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "last_alert_at": self._last_alert_at,
            }

    def _run(self, generation: int, username: str, password: str) -> None:
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            sock = None
            try:
                sock = socket.create_connection((HAMALERT_HOST, HAMALERT_PORT), timeout=15)
                sock.sendall((username + "\r\n").encode())
                sock.sendall((password + "\r\n").encode())
                sock.sendall(b"set/json\r\n")
                sock.settimeout(RECV_TIMEOUT)

                with self._lock:
                    if generation != self._generation:
                        sock.close()
                        return
                    self._connected = True

                buf = b""
                last_heartbeat = time.time()
                while True:
                    with self._lock:
                        if generation != self._generation:
                            sock.close()
                            return
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        chunk = None
                    if chunk == b"":
                        break  # server closed the connection
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._handle_line(line.decode("utf-8", "replace").strip())
                    if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                        sock.sendall(b"echo hb\r\n")
                        last_heartbeat = time.time()
            except OSError:
                pass
            finally:
                with self._lock:
                    self._connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            with self._lock:
                if generation != self._generation:
                    return
            time.sleep(RECONNECT_BACKOFF)

    def _handle_line(self, line: str) -> None:
        """Every line other than a real JSON spot object is ignored --
        this includes the `hb` heartbeat echo reply and any stray
        banner/whitespace, since a plain json.loads() failure or a
        missing "callsign" key both just fall through silently. Never
        raises, matching every other integration's degrade-gracefully
        contract in this project."""
        if not line:
            return
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or not data.get("callsign"):
            return

        alert = {
            "callsign": data.get("callsign"),
            "full_callsign": data.get("fullCallsign") or data.get("callsign"),
            "band": data.get("band"),
            "mode": data.get("mode"),
            "frequency": data.get("frequency"),
            "spotter": data.get("spotter"),
            "source": data.get("source"),
            "entity": data.get("entity"),
            "comment": data.get("comment"),
            "trigger_comment": data.get("triggerComment"),
            "received_at": time.time(),
        }
        with self._lock:
            self._alerts.appendleft(alert)
            self._last_alert_at = alert["received_at"]
