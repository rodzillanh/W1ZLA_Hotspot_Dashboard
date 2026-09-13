"""Persistent Telnet connection to the Reverse Beacon Network's live spot
feed (telnet.reversebeacon.net:7000) for the Spots card's RBN filter --
same "one persistent connection, reconnect with backoff, reconfigured in
place via a generation counter" shape as hamalert.py/aprs_inbox.py, adapted
for RBN's much simpler login (no password) and plain fixed-shape text
lines instead of JSON.

Confirmed live (2026-09, from this dev environment -- RBN is fully public,
no allowlist issue like some other integrations have hit): connecting and
sending a bare callsign logs you in immediately, no password of any kind.
A real captured sample of the live feed, right after login:

    DX de S50U-#:   14024.50  EU1TN          CW     7 dB  23 WPM  CQ      1516Z
    DX de RN4WA-#:  14091.60  IZ0FVD         RTTY  29 dB  45 BPS  CQ      1516Z
    DX de ES2RR-#:  21149.00  IT9ATQ/B       CW    13 dB  10 WPM  BEACON  1516Z
    DX de KM3T-2-#: 14050.00  K9HG           CW    13 dB  12 WPM  CQ      1516Z

Fields, left to right: the CW/RTTY skimmer's own callsign (always suffixed
`-#`, confirmed on every line seen -- this is how a real DX-cluster human
spot would be told apart from an automated skimmer one, though this module
only ever sees skimmer lines since that's all RBN forwards on this feed),
frequency in kHz, the spotted station's callsign (a portable/beacon suffix
like `/B` rides along as part of this token, not parsed out), mode (CW or
RTTY were the only two seen in an 8-second live capture -- PSK/other digital
skimmers exist on RBN too but weren't observed; the regex below accepts any
bare word here rather than a fixed set), SNR in dB, keying speed (WPM for
CW, BPS for RTTY -- genuinely different units per mode, not normalized),
a free-text tag (only "CQ" and "BEACON" seen live; treated as an opaque
string, not a fixed enum), and a UTC HHMM timestamp. Column widths are
padded with spaces and vary, so this is parsed with a regex, not a fixed-
width slice.

Confirmed live at the same time: RBN's volume is genuinely a firehose, not
an exaggeration -- the login banner itself reports the current global spot
rate ("Spot rate: 6/s (20,498/h)" at capture time). This is exactly why
`RbnListener` dedupes in place (same call within a small frequency window
replaces the existing entry and refreshes its age, rather than piling up
a new line every time a different skimmer hears the same signal) and
prunes anything older than STALE_SECONDS on every read, instead of simply
accumulating into an ever-growing deque the way hamalert.py's alert
history does -- an RBN spot list has no "keep forever" value the way a
HamAlert trigger match does, it's only ever "what's on the air right now".
"""
import re
import socket
import threading
import time

RBN_HOST = "telnet.reversebeacon.net"
RBN_PORT = 7000
RECV_TIMEOUT = 60
RECONNECT_BACKOFF = 15
STALE_SECONDS = 600       # an RBN spot not re-heard in 10 min is gone, not just "old"
DEDUPE_FREQ_KHZ = 2.0     # same call within this window collapses into one row
MAX_SPOTS = 400           # hard cap on the live dict even before staleness pruning kicks in

_SPOT_RE = re.compile(
    r"^DX de (\S+):\s+(\d+(?:\.\d+)?)\s+(\S+)\s+(\w+)\s+(\d+)\s*dB\s+"
    r"(\d+)\s*(WPM|BPS)\s+(\S+)\s+(\d{4})Z\s*$"
)


def _parse_line(line: str) -> "dict | None":
    m = _SPOT_RE.match(line.strip())
    if not m:
        return None
    spotter, freq_khz, call, mode, snr, speed, speed_unit, tag, hhmm = m.groups()
    try:
        freq_khz = float(freq_khz)
    except ValueError:
        return None
    return {
        "spotter": spotter,
        "freq_khz": freq_khz,
        "call": call,
        "mode": mode.upper(),
        "snr_db": int(snr),
        "speed": int(speed),
        "speed_unit": speed_unit,
        "tag": tag.upper(),
        "hhmm": hhmm,
    }


class RbnListener:
    """Reconfigured in place via a generation counter -- same pattern as
    hamalert.py's HamAlertListener. A stale thread notices the generation
    changed (checked before every blocking recv) and exits cleanly instead
    of running forever under a stale/disabled config."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._enabled = False
        self._callsign = ""
        self._spots = {}          # (call, freq_bucket) -> spot dict
        self._connected = False
        self._last_error = None

    def configure(self, enabled: bool, callsign: str) -> None:
        callsign = (callsign or "").strip().upper()
        with self._lock:
            if enabled == self._enabled and callsign == self._callsign:
                return  # no-op, cheap to call on every settings save
            self._enabled = enabled
            self._callsign = callsign
            self._generation += 1
            generation = self._generation
            if not enabled:
                self._connected = False
        if enabled and callsign:
            threading.Thread(target=self._run, args=(generation, callsign), daemon=True).start()

    @staticmethod
    def test_connection(callsign: str, timeout: float = 8.0) -> tuple:
        """Settings 'Test connection' button -- a fresh, one-off login,
        no persistent listener touched. RBN's own login banner explicitly
        greets you by callsign ("Hello, <CALL>! Connected.") -- confirmed
        live -- so unlike hamalert.py's ambiguous "socket didn't close"
        heuristic, this one has a real, positive success message to look
        for."""
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return False, "Callsign required"
        try:
            sock = socket.create_connection((RBN_HOST, RBN_PORT), timeout=10)
        except OSError as e:
            return False, f"Could not reach {RBN_HOST}: {e}"
        try:
            sock.sendall((callsign + "\r\n").encode())
            sock.settimeout(timeout)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                chunk = b""
            text = chunk.decode("utf-8", "replace")
            if f"Hello, {callsign}" in text or "Connected" in text:
                return True, f"Connected to RBN as {callsign}"
            if chunk == b"":
                return False, "Connection closed immediately -- RBN may be rejecting this callsign"
            return True, "Connected -- no explicit greeting seen, but the socket stayed open"
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def recent(self, band_khz_lo: "float | None" = None, band_khz_hi: "float | None" = None) -> list:
        """Live spots, freshest first, with stale entries pruned. Pass a
        kHz range to filter by band; either bound alone also works."""
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
            }

    def _run(self, generation: int, callsign: str) -> None:
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            sock = None
            try:
                sock = socket.create_connection((RBN_HOST, RBN_PORT), timeout=15)
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
                        continue  # RBN has no need for our own keepalive -- it's constantly sending
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
                # Drop the stalest entries first rather than an arbitrary/insertion-order one.
                oldest = sorted(self._spots.items(), key=lambda kv: kv[1]["last_seen"])[: len(self._spots) - MAX_SPOTS]
                for k, _ in oldest:
                    self._spots.pop(k, None)
