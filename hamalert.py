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

Since March 2024, HamAlert supports a dedicated Telnet password (set on
hamalert.org's own Destinations page), separate from the main website/
app login password -- confirmed directly from the developer (HB9DQM) on
the support forum: "I have just added the option to set a separate
Telnet password on the Destinations page. This can only be used to
login to the Telnet interface, not to the website or app."
(forum.hamalert.org/t/api-key-generation-limited-access/683). This
module's `configure()`/`test_connection()` just pass through whatever
password the user supplies -- it's Settings' UI copy that's responsible
for telling the user to use that dedicated Telnet password rather than
their main account password, not this module.

Confirmed live against a real account (2026-07-26, a POTA-sourced test
match): `triggerComment` comes back as an EMPTY LIST (`[]`), not `null`
or an absent key, when a match has no specific trigger reason attached
-- NOT the plain string this module originally assumed from the
WhiskeyTangoHotel/forum sources alone. An empty list is truthy in JS, so
passing it straight through to the frontend rendered an empty, blank
"trigger" pill on every alert without one. `_handle_line()` normalizes
this server-side (joins a non-empty list, or `None`) so the frontend
only ever sees a real string or nothing.
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
        self._last_error = None

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

    @staticmethod
    def test_connection(username: str, password: str, timeout: float = 8.0) -> tuple:
        """Settings 'Test connection' button -- a fresh, one-off login
        attempt, no persistent listener touched (mirrors mqtt_publisher.py's/
        openspot.py's own test_connection() shape). HamAlert's Telnet
        interface has no documented explicit login-accepted/-rejected
        message (confirmed via its own support forum, forum.hamalert.org/
        t/documentation-for-telnet-interface/682) -- the socket staying
        open after login, rather than the server closing it, is the best
        available success signal here. NOT verified against a real
        invalid-login case (no real account available in this dev
        environment) -- if this ever reports a false positive/negative,
        re-check against a live account before assuming the heuristic
        itself is wrong, same discipline as every other unverified corner
        of this module."""
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            return False, "Username and password required"
        try:
            sock = socket.create_connection((HAMALERT_HOST, HAMALERT_PORT), timeout=10)
        except OSError as e:
            return False, f"Could not reach {HAMALERT_HOST}: {e}"
        try:
            sock.sendall((username + "\r\n").encode())
            sock.sendall((password + "\r\n").encode())
            sock.sendall(b"set/json\r\n")
            sock.settimeout(timeout)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                chunk = None  # no data within timeout, but socket still open -- treated as success
            if chunk == b"":
                return False, ("Connection closed right after login -- check the username and "
                                "Telnet password (hamalert.org -> Destinations; a separate "
                                "password from your website/app login)")
            return True, "Connected -- login was not rejected"
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def recent(self) -> list:
        with self._lock:
            return list(self._alerts)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "last_alert_at": self._last_alert_at,
                "last_error": self._last_error,
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
                    self._last_error = None

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
                        with self._lock:
                            self._last_error = ("Server closed the connection -- likely a rejected "
                                                 "login (wrong username/Telnet password)")
                        break  # server closed the connection
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._handle_line(line.decode("utf-8", "replace").strip())
                    if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                        sock.sendall(b"echo hb\r\n")
                        last_heartbeat = time.time()
            except OSError as e:
                with self._lock:
                    self._last_error = str(e)
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

        # triggerComment comes back as an EMPTY LIST (not null/absent) when
        # a match had no specific trigger reason -- confirmed live against a
        # real account. Normalize to a plain string or None here so the
        # frontend never has to special-case "truthy but empty" JS arrays.
        trigger_comment = data.get("triggerComment")
        if isinstance(trigger_comment, list):
            trigger_comment = ", ".join(str(x) for x in trigger_comment if x) or None

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
            "trigger_comment": trigger_comment,
            "received_at": time.time(),
        }
        with self._lock:
            self._alerts.appendleft(alert)
            self._last_alert_at = alert["received_at"]
