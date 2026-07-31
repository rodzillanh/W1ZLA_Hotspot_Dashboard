"""Live QSO logging from WSJT-X over its own UDP telemetry protocol --
the same feed GridTracker/JTAlert listen to, not a new protocol invented
here. Message field order/types were confirmed against bmo/py-wsjtx's
real, working open-source implementation
(github.com/bmo/py-wsjtx/blob/master/pywsjtx/wsjtx_packets.py) before
writing this, not from memory or WSJT-X's own C++ source comments alone.
This module was NOT live-tested against a real running WSJT-X instance
(no such device reachable from this dev environment) -- if it ever
silently stops picking up QSOs, re-verify field order/types against a
real packet capture before assuming the parser is still correct, same
"verify against the real thing" discipline as openspot.py/digipi.py
elsewhere in this project.

Deliberately only handles the QSOLogged message type (5), not the much
chattier Decode message (every single decode attempt, most without a
usable grid square, many per second across a wide waterfall). Streaming
every decode would reproduce exactly the "worldwide spotter clutter" the
Live map's WSPR spots overlay was removed for. QSOLogged fires once per
actually-logged QSO with clean structured fields -- functionally one
ADIF record's worth of data, arriving live instead of in a bulk file.
Heartbeat (type 0) is recognized only to update "last packet seen" for
the Settings status line -- proof the listener is actually receiving
something from WSJT-X even between logged QSOs.

WSJT-X's own grid square (its "my_grid" field, from WSJT-X's own
configured station location) is used as the QTH for that QSO's map line
-- the same qth_lat/qth_lon field the ADIF importer's MY_GRIDSQUARE
handling already produces (see app.py's api_import_adif), so the
frontend's existing QSO rendering code needs no changes to show these.
"""
import math
import socket
import struct
import threading
import time

from storage import append_qso, load_settings

MAGIC_NUMBER = 0xadbccbda
MIN_SCHEMA = 2
MAX_SCHEMA = 3
TYPE_HEARTBEAT = 0
TYPE_QSO_LOGGED = 5

# Standard amateur band edges in Hz -- matches templates/dashboard.html's
# QSO_BAND_COLORS keys. WSJT-X's QSOLoggedPacket only reports a raw
# frequency, not a band label the way ADIF's own BAND field already is.
_BAND_EDGES = [
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
    ("2m", 144_000_000, 148_000_000),
]


def freq_to_band(hz: int) -> str:
    for label, lo, hi in _BAND_EDGES:
        if lo <= hz <= hi:
            return label
    return ""


def _julian_to_ymd(jdnum: int) -> tuple[int, int, int]:
    """Julian day number -> (year, month, day). Ported from py-wsjtx's
    JDToDateMeeus (itself a standard Meeus algorithm) -- only the date is
    needed here (for the QSO's date string), not the intraday time."""
    jd = jdnum + 0.5
    z = jd
    f = jd - z
    if z < 2299161:
        a = z
    else:
        alpha = math.floor((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - math.floor(alpha / 4.0)
    b = a + 1524
    c = math.floor((b - 122.1) / 365.25)
    d = math.floor(365.25 * c)
    e = math.floor((b - d) / 30.6001)
    day = int(b - d - math.floor(30.6001 * e) + f)
    month = int(e - 1 if e < 14 else e - 13)
    year = int(c - 4716 if month > 2 else c - 4715)
    return year, month, day


class _Reader:
    """Minimal big-endian QDataStream-style reader -- only the field
    types QSOLoggedPacket/HeartBeatPacket actually use."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def i32(self) -> int:
        v = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i64(self) -> int:
        v = struct.unpack_from(">q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def i8(self) -> int:
        v = struct.unpack_from(">b", self.data, self.pos)[0]
        self.pos += 1
        return v

    def qstring(self) -> str:
        length = self.i32()
        if length <= 0:
            return ""
        s = self.data[self.pos:self.pos + length].decode("utf-8", "replace")
        self.pos += length
        return s

    def qdate(self) -> tuple[int, int, int]:
        """Reads a QDateTime, returning only its (year, month, day) --
        the intraday time/timespec/UTC-offset fields aren't needed for
        the QSO's date string and are consumed here just to advance the
        stream position correctly for whatever field follows."""
        jdnum = self.i64()
        self.i32()  # millis since midnight, unused
        spec = self.i8()
        if spec == 2:
            self.i32()  # UTC offset seconds, unused
        return _julian_to_ymd(jdnum)


def parse_packet(data: bytes) -> dict | None:
    """Parses one WSJT-X UDP datagram. Returns {"type": "heartbeat"} for
    a Heartbeat packet (just proof of life), a QSO dict for QSOLogged, or
    None for anything else (Status/Decode/Clear/etc -- not needed here)
    or malformed data. Never raises -- same degrade-gracefully contract
    as every other integration client in this project."""
    try:
        if len(data) < 12:
            return None
        magic, schema, pkt_type = struct.unpack_from(">III", data, 0)
        if magic != MAGIC_NUMBER or not (MIN_SCHEMA <= schema <= MAX_SCHEMA):
            return None
        if pkt_type == TYPE_HEARTBEAT:
            return {"type": "heartbeat"}
        if pkt_type != TYPE_QSO_LOGGED:
            return None

        r = _Reader(data, 12)
        r.qstring()  # wsjtx_id (which WSJT-X instance), unused
        year, month, day = r.qdate()  # datetime_off
        call = r.qstring()
        grid = r.qstring()
        frequency = r.i64()
        mode = r.qstring()
        report_sent = r.qstring()
        report_recv = r.qstring()
        r.qstring()  # tx_power, unused
        r.qstring()  # comments, unused
        name = r.qstring()
        r.qdate()    # datetime_on, unused
        r.qstring()  # op_call, unused
        r.qstring()  # my_call, unused
        my_grid = r.qstring()
        # exchange_sent/exchange_recv follow but aren't needed -- stop here.

        call = call.strip().upper()
        if not call:
            return None
        return {
            "type": "qso",
            "call": call,
            "grid": grid.strip(),
            "band": freq_to_band(frequency),
            "frequency_hz": frequency,
            "mode": mode.strip().upper(),
            "date": f"{year:04d}{month:02d}{day:02d}",
            "my_grid": my_grid.strip(),
            "rst_sent": report_sent.strip(),
            "rst_rcvd": report_recv.strip(),
            "name": name.strip(),
        }
    except (struct.error, IndexError, UnicodeDecodeError):
        return None


class WsjtxListener:
    """One persistent UDP socket, reconfigured in place via a generation
    counter -- same pattern as aprs_inbox.py's AprsInbox, adapted for a
    connectionless protocol: there's no login/reconnect-with-backoff to
    manage, closing the socket is enough to unblock a stale thread's
    blocking recvfrom() immediately (no missed-notification race the way
    aprs_inbox.py's threading.Condition needed to guard against)."""

    def __init__(self, monitor):
        self._monitor = monitor
        self._lock = threading.Lock()
        self._generation = 0
        self._enabled = False
        self._port = 2237
        self._last_packet_at = None
        self._last_qso = None

    def configure(self, enabled: bool, port: int) -> None:
        with self._lock:
            if enabled == self._enabled and port == self._port:
                return  # no-op, cheap to call on every settings save
            self._enabled = enabled
            self._port = port
            self._generation += 1
            generation = self._generation
        if enabled:
            threading.Thread(target=self._run, args=(generation, port), daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "port": self._port,
                "last_packet_at": self._last_packet_at,
                "last_qso": self._last_qso,
            }

    def _run(self, generation: int, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", port))
            sock.settimeout(1.0)  # so a generation change is noticed promptly, not just on the next packet
        except OSError:
            return  # port in use / no permission -- status() keeps reporting no last_packet_at, readable as "not receiving"

        while True:
            with self._lock:
                if generation != self._generation:
                    break
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            with self._lock:
                if generation != self._generation:
                    break

            parsed = parse_packet(data)
            if parsed is None:
                continue
            self._last_packet_at = time.time()
            if parsed["type"] == "qso":
                self._last_qso = parsed
                self._handle_qso(parsed)

        try:
            sock.close()
        except OSError:
            pass

    def _handle_qso(self, parsed: dict) -> None:
        """Resolves a position the same way the ADIF importer does
        (grid square first, QRZ/RadioID/APRS lookup fallback via
        monitor.lookup_caller_info) and appends one QSO to qsos.json --
        never raises, so a single bad/unresolvable packet never takes
        the listener thread down."""
        try:
            from wspr_activity import grid_to_latlon  # local import avoids a hard dependency cycle at module load time

            # Always attempt the QRZ/RadioID/APRS composition now, not just
            # as a position fallback when the grid square is missing --
            # name/city/state/country are display-only extras for the
            # Recent Contacts card, worth fetching (QRZ's own client already
            # caches per callsign) even when the grid square alone already
            # gives us a usable position.
            info = self._monitor.lookup_caller_info(parsed["call"])
            # WSJT-X's own logged "name" field (rare -- only populated if
            # the other station's software sent one, e.g. via a free-text
            # exchange) takes priority over QRZ's when present, same
            # "what was actually logged at QSO time beats a lookup"
            # priority as the ADIF importer's own COUNTRY field over QRZ's.
            name = parsed["name"] or info["name"]
            location = info["location"]
            city, state, country = info["city"], info["state"], info["country"]

            latlon = grid_to_latlon(parsed["grid"]) if parsed["grid"] else None
            lat, lon = latlon if latlon is not None else (info["lat"], info["lon"])
            if lat is None or lon is None:
                return  # can't plot without a position, same as the ADIF importer

            # WSJT-X's own configured grid square is almost always present
            # (it's a required field in WSJT-X's own settings dialog), but
            # fall back to this dashboard's station_grid setting for the
            # rare case it's blank -- same priority order as the ADIF
            # importer's MY_GRIDSQUARE handling.
            qth = grid_to_latlon(parsed["my_grid"]) if parsed["my_grid"] else None
            if qth is None:
                qth = grid_to_latlon(load_settings().get("station_grid", ""))
            qth_lat, qth_lon = qth if qth is not None else (None, None)

            append_qso({
                "call": parsed["call"],
                "band": parsed["band"],
                "mode": parsed["mode"],
                "date": parsed["date"],
                "grid": parsed["grid"] or None,
                "frequency_hz": parsed["frequency_hz"],
                "lat": lat, "lon": lon,
                "qth_lat": qth_lat, "qth_lon": qth_lon,
                "name": name, "location": location,
                "city": city, "state": state, "country": country,
                "rst_sent": parsed["rst_sent"] or None,
                "rst_rcvd": parsed["rst_rcvd"] or None,
                "source": "wsjtx",
                "logged_at": time.time(),  # epoch seconds -- lets the map
                # highlight a QSO as "just happened" for a while, then fade
                # it back to the normal band-colored pin (dashboard.html's
                # QSO_NEW_WINDOW_SEC), and lets the Recent Contacts card
                # sort by actual time. Bulk ADIF imports have their own
                # equivalent derived from QSO_DATE+TIME_ON (app.py).
            })
        except Exception:
            pass
