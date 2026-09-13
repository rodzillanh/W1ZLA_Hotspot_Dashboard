"""Persistent Telnet connection to a classic packet DX cluster (DXSpider/
AR-Cluster/CC-Cluster -- the same AK1A-descended wire protocol every
flavor speaks) for the Spots card's optional "DX" filter -- same overall
shape as rbn.py, but genuinely different in two ways: the host is
user-configurable (DX clusters are a federated network, unlike RBN's one
global feed -- there is no single "the" cluster to hardcode), and the
spot line's trailing field is free-text typed by a human operator, not a
fixed machine-generated schema the way RBN's own dB/WPM/tag fields are.

Confirmed live (2026-09, three real nodes near this dev environment's
configured station, all fully reachable, no allowlist issue):

    dx.w1nr.net:7300     (W1NR, Hudson MA -- DXSpider, YCCC network,
                          RBN-merge available via SET/SKIMMER but OFF by
                          default, so a fresh login is pure human spots)
    usdx.w1nr.net:7300   (W1NR-9, same operator, pre-filtered by the node
                          itself to CQ zones 2-8 -- confirmed from its own
                          live banner text, not the secondhand "1-8"
                          description this was first described with)
    k1ttt.net:7373       (K1TTT -- AR-Cluster, a different codebase
                          entirely; its own banner states explicitly "No
                          RBN spots are available on this node")

All three, despite being different software from different operators,
emit the IDENTICAL spot line shape RBN's own feed uses:

    DX de PY1AX:     21273.6  HG8L         USB                            1554Z
    DX de YB7OMZ:    21234.0  YC7ONI       cq dx                          1554Z
    DX de IT9JOB:     7096.0  II9IABJ      SSB Italian Navy Ship          1555Z
    DX de PU1JSV:    28470.0  SN7D                                        1551Z

-- confirming this really is one shared, decades-old convention (AK1A's
original PacketCluster format), not something specific to RBN. The
difference that actually matters for parsing: RBN's trailing field is a
fixed, machine-generated schema (mode/dB/speed/tag, all present, all in
the same order); a human-typed cluster spot's trailing field is
completely free text -- sometimes a bare mode ("USB"), sometimes a
comment with no mode at all ("cq dx", "Italian Navy Ship"), sometimes
nothing. `_parse_line()` reflects this honestly: `mode` is populated only
when the FIRST word of the comment matches a recognized mode token,
`comment` always carries the raw trailing text verbatim (so a genuinely
useful human note like "Italian Navy Ship" isn't silently discarded even
when no mode could be recognized).

Login is the same shape as rbn.py -- send a bare callsign, no password --
but a real greeting banner is NOT a fixed string to check against here
the way RBN's "Hello, <CALL>!" is: DXSpider says "Hello Rodney, this is
W1NR..." (confirmed live it resolves a registered name, not just the
callsign), AR-Cluster says "Hello   W1ZLA" -- two different softwares,
two different phrasings, and the host itself is user-configurable so a
THIRD phrasing is always possible. `test_connection()` therefore uses the
same "did the socket stay open" heuristic hamalert.py's own unverified-
banner case uses, not a string match.
"""
import re
import socket
import threading
import time

RECV_TIMEOUT = 60
RECONNECT_BACKOFF = 15
STALE_SECONDS = 600
DEDUPE_FREQ_KHZ = 2.0
MAX_SPOTS = 400
DEFAULT_HOST = "dx.w1nr.net:7300"  # W1NR (Hudson, MA) -- confirmed live,
                                    # active, RBN-merge off by default so
                                    # a fresh login is pure human spots

_MODE_TOKENS = {
    "CW", "SSB", "USB", "LSB", "FM", "AM", "RTTY", "FT8", "FT4", "FT4W",
    "PSK", "PSK31", "PSK63", "DIGITAL", "DATA", "JS8", "OLIVIA", "MFSK", "SSTV",
}

_SPOT_RE = re.compile(
    r"^DX de (\S+):\s+(\d+(?:\.\d+)?)\s+(\S+)\s*(.*?)\s*(\d{4})Z\s*$"
)


def _split_host_port(host_port: str, default_port: int = 7300) -> "tuple | None":
    host_port = (host_port or "").strip()
    if not host_port:
        return None
    if ":" in host_port:
        host, _, port_s = host_port.rpartition(":")
        try:
            return host, int(port_s)
        except ValueError:
            return None
    return host_port, default_port


def _parse_line(line: str) -> "dict | None":
    m = _SPOT_RE.match(line.strip())
    if not m:
        return None
    spotter, freq_khz, call, comment, hhmm = m.groups()
    try:
        freq_khz = float(freq_khz)
    except ValueError:
        return None
    mode = ""
    comment = comment.strip()
    if comment:
        first, _, rest = comment.partition(" ")
        if first.upper() in _MODE_TOKENS:
            mode = first.upper()
    return {
        "spotter": spotter,
        "freq_khz": freq_khz,
        "call": call,
        "mode": mode,
        "comment": comment,
        "hhmm": hhmm,
    }


class DxClusterListener:
    """Reconfigured in place via a generation counter -- same pattern as
    rbn.py's RbnListener/hamalert.py's HamAlertListener. Unlike RBN, the
    host itself is part of what configure() compares, since it's a
    user-editable field, not a fixed constant."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._enabled = False
        self._host_port = ""
        self._callsign = ""
        self._spots = {}
        self._connected = False
        self._last_error = None

    def configure(self, enabled: bool, host_port: str, callsign: str) -> None:
        host_port = (host_port or "").strip()
        callsign = (callsign or "").strip().upper()
        with self._lock:
            if enabled == self._enabled and host_port == self._host_port and callsign == self._callsign:
                return
            self._enabled = enabled
            self._host_port = host_port
            self._callsign = callsign
            self._generation += 1
            generation = self._generation
            if not enabled:
                self._connected = False
        if enabled and host_port and callsign:
            threading.Thread(target=self._run, args=(generation, host_port, callsign), daemon=True).start()

    @staticmethod
    def test_connection(host_port: str, callsign: str, timeout: float = 8.0) -> tuple:
        """Settings 'Test connection' button. No fixed greeting string to
        check for -- different cluster software phrases its banner
        differently (confirmed live: DXSpider vs. AR-Cluster already
        disagree on this), so this is the same "socket stayed open"
        heuristic hamalert.py's own unverified case uses, not a match
        against specific text."""
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return False, "Callsign required"
        hp = _split_host_port(host_port)
        if hp is None:
            return False, "Host required (e.g. dx.w1nr.net:7300)"
        host, port = hp
        try:
            sock = socket.create_connection((host, port), timeout=10)
        except OSError as e:
            return False, f"Could not reach {host}:{port}: {e}"
        try:
            sock.sendall((callsign + "\r\n").encode())
            sock.settimeout(timeout)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                chunk = b""
            if chunk == b"":
                return False, "Connection closed immediately -- check the host/port"
            return True, f"Connected to {host}:{port}"
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def recent(self, band_khz_lo: "float | None" = None, band_khz_hi: "float | None" = None) -> list:
        now = time.time()
        with self._lock:
            self._spots = {k: v for k, v in self._spots.items() if now - v["last_seen"] < STALE_SECONDS}
            spots = list(self._spots.values())
        if band_khz_lo is not None:
            spots = [s for s in spots if s["freq_khz"] >= band_khz_lo]
        if band_khz_hi is not None:
            spots = [s for s in spots if s["freq_khz"] <= band_khz_hi]
        spots.sort(key=lambda s: s["last_seen"], reverse=True)
        return spots

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "last_error": self._last_error,
                "count": len(self._spots),
                "host": self._host_port,
            }

    def _run(self, generation: int, host_port: str, callsign: str) -> None:
        hp = _split_host_port(host_port)
        if hp is None:
            with self._lock:
                self._last_error = "Invalid host"
            return
        host, port = hp
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=15)
                sock.sendall((callsign + "\r\n").encode())
                sock.settimeout(RECV_TIMEOUT)

                with self._lock:
                    if generation != self._generation:
                        sock.close()
                        return
                    self._connected = True
                    self._last_error = None

                buf = b""
                while True:
                    with self._lock:
                        if generation != self._generation:
                            sock.close()
                            return
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if chunk == b"":
                        with self._lock:
                            self._last_error = "Server closed the connection"
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", "replace"))
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
        parsed = _parse_line(line)
        if parsed is None:
            return
        bucket = round(parsed["freq_khz"] / DEDUPE_FREQ_KHZ) * DEDUPE_FREQ_KHZ
        key = (parsed["call"], bucket)
        now = time.time()
        with self._lock:
            existing = self._spots.get(key)
            first_seen = existing["first_seen"] if existing else now
            parsed["first_seen"] = first_seen
            parsed["last_seen"] = now
            self._spots[key] = parsed
            if len(self._spots) > MAX_SPOTS:
                oldest = sorted(self._spots.items(), key=lambda kv: kv[1]["last_seen"])[: len(self._spots) - MAX_SPOTS]
                for k, _ in oldest:
                    self._spots.pop(k, None)
