"""ircDDBGateway remote control client -- read a WPSD D-STAR hotspot's
current reflector link and link/unlink it, for the hotspot card's
"D-STAR (ircDDBGateway)" drawer section.

Unlike every other reverse-engineered protocol in this project (DVSwitch,
ASL3's RPT_ALINKS, Brandmeister's Last Heard feed), this one has real,
public GPLv2 source to read directly rather than guess from traffic:
github.com/g4klx/ircddbgateway, specifically Common/RemoteHandler.cpp +
RemoteProtocolHandler.cpp + Defs.h + DStarDefines.h -- the actual shipped
daemon, not a summary of it. Confirmed against a real, live WPSD D-STAR
hotspot's deployed config:

    sudo grep -A2 -i remote /etc/ircddbgateway
        remoteEnabled=1
        remotePassword=<password>
        remotePort=<port>              (repo's own template default: 54321)
    sudo ss -ulnp | grep <port>
        UNCONN ... 0.0.0.0:<port> ... users:(("ircddbgatewayd",...))

-- i.e. it's plain UDP, listening on all interfaces (LAN-reachable, not
loopback-only), so this app can talk to it directly over the network
the same way it talks to rigctld/DVSwitch's Analog_Bridge, no SSH needed.

Wire protocol, read straight from RemoteProtocolHandler.cpp (all
multi-byte integers little-endian -- wxUINT32_SWAP_ON_BE is a no-op on
every little-endian target this runs on, ARM Pi included; flip the
struct format to '>' if a real device ever proves otherwise):

    ->  b"LIN"
    <-  b"RND" + <4-byte random>
    ->  b"SHA" + SHA256(<4-byte random> + password bytes)      (32 bytes)
    <-  b"ACK"  or  b"NAK" + text
    ->  b"GRP" + <8-byte callsign>                              (status)
    <-  b"RPT" + <8-byte callsign> + <4-byte reconnect> + <8-byte reflector>
        + zero or more 24-byte link blocks:
            <8-byte callsign><4-byte protocol><4-byte linked>
            <4-byte direction><4-byte dongle>
    ->  b"LNK" + <8-byte callsign> + <4-byte reconnect> + <8-byte reflector>
    <-  b"ACK"  or  b"NAK" + text                                (link)
    ->  b"UNL" + <8-byte callsign> + <4-byte protocol> + <8-byte reflector>
    <-  b"ACK"  or  b"NAK" + text                                (unlink)
    ->  b"LOG"                                                   (logout)

Every callsign/reflector field is exactly 8 bytes, upper-cased and
space-padded -- D-STAR's standard width (a reflector+module address like
"REF030 C" is exactly 8 characters). `callsign` throughout this module
is ircDDBGateway's own configured DV repeater callsign for the hotspot
(its `repeaterCall1` in /etc/ircddbgateway) -- for a hotspot this is
often, but not necessarily, formatted with a trailing module letter, so
it's a per-hotspot setting here, not assumed to equal the plain callsign.

Short-lived UDP "session" per call (open socket, LIN->RND->SHA->ACK, do
the one thing, LOG, close) -- same shape as rigctl.py's connect-per-tune
path, not a persistent connection. Never raises out of its public
methods; every failure comes back as None / (False, message).

Built entirely from source plus the real config dump above -- NOT yet
verified with a live packet exchange against a real ircddbgatewayd. The
first real test is Settings' "Test connection" button (test()) /
opening the drawer (status()); if login fails outright, byte order
(try '>' instead of '<' in the struct formats) is the first thing to
suspect on a big-endian host, though none of this app's supported
targets (Pi ARM, x86) are big-endian.
"""
import hashlib
import socket
import struct

TIMEOUT = 4.0
FIELD_LEN = 8  # LONG_CALLSIGN_LENGTH in the real source

RECONNECT_NAMES = [
    "Never", "Fixed", "5 min", "10 min", "15 min", "20 min", "25 min",
    "30 min", "60 min", "90 min", "120 min", "180 min",
]
PROTOCOL_NAMES = ["DExtra", "DPlus", "DCS", "CCS"]


def _pad(s):
    return (s or "").strip().upper().ljust(FIELD_LEN)[:FIELD_LEN].encode("ascii", "replace")


def _unpad(b):
    return b.decode("ascii", "replace").strip()


def _recv(sock):
    try:
        return sock.recv(2048)
    except OSError:
        return None


def _open(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    sock.connect((host, int(port)))
    return sock


def _close(sock):
    try:
        sock.sendall(b"LOG")
    except OSError:
        pass
    sock.close()


def _login(sock, password):
    """Raises OSError (caught by every public method below) on any
    network problem or a rejected password."""
    sock.sendall(b"LIN")
    rnd = _recv(sock)
    if not rnd or not rnd.startswith(b"RND") or len(rnd) < 7:
        raise OSError("No login challenge received (check host/port)")
    random_val = struct.unpack("<I", rnd[3:7])[0]
    digest = hashlib.sha256(struct.pack("<I", random_val) + (password or "").encode("ascii", "replace")).digest()
    sock.sendall(b"SHA" + digest)
    ack = _recv(sock)
    if not ack or not ack.startswith(b"ACK"):
        reason = "no reply"
        if ack and ack.startswith(b"NAK"):
            reason = ack[3:].split(b"\x00")[0].decode("ascii", "replace") or "rejected"
        raise OSError(f"Login failed: {reason}")


def _ack_result(resp, ok_message):
    if resp and resp.startswith(b"ACK"):
        return True, ok_message
    if resp and resp.startswith(b"NAK"):
        reason = resp[3:].split(b"\x00")[0].decode("ascii", "replace") or "rejected"
        return False, reason
    return False, "No response from ircDDBGateway"


def _parse_status(resp):
    if len(resp) < 23:
        return None
    callsign = _unpad(resp[3:11])
    reconnect = struct.unpack("<i", resp[11:15])[0]
    reflector = _unpad(resp[15:23]) or None
    links = []
    off = 23
    while off + 24 <= len(resp):
        link_call = _unpad(resp[off:off + 8])
        protocol, linked, direction, dongle = struct.unpack("<iiii", resp[off + 8:off + 24])
        links.append({
            "callsign": link_call,
            "protocol": PROTOCOL_NAMES[protocol] if 0 <= protocol < len(PROTOCOL_NAMES) else str(protocol),
            "protocol_index": protocol,
            "linked": bool(linked),
            "direction": "outgoing" if direction == 1 else "incoming",
            "dongle": bool(dongle),
        })
        off += 24
    return {
        "callsign": callsign,
        "reflector": reflector,
        "reconnect": reconnect,
        "reconnect_label": RECONNECT_NAMES[reconnect] if 0 <= reconnect < len(RECONNECT_NAMES) else str(reconnect),
        "links": links,
    }


class IrcddbGatewayClient:
    def status(self, host, port, password, callsign):
        """Current D-STAR link state for `callsign` (ircDDBGateway's own
        configured repeater callsign). Returns a dict or None on any
        failure -- never raises."""
        try:
            sock = _open(host, port)
        except OSError:
            return None
        try:
            _login(sock, password)
            sock.sendall(b"GRP" + _pad(callsign))
            resp = _recv(sock)
            if not resp or not resp.startswith(b"RPT"):
                return None
            return _parse_status(resp)
        except OSError:
            return None
        finally:
            _close(sock)

    def link(self, host, port, password, callsign, reflector, reconnect=0):
        """Link `callsign` to `reflector` (an 8-char D-STAR address, e.g.
        "REF030 C"). `reconnect` is an index into RECONNECT_NAMES (0 =
        never auto-reconnect). Returns (ok, message)."""
        try:
            sock = _open(host, port)
        except OSError as e:
            return False, f"Can't reach ircDDBGateway at {host}:{port} ({e})"
        try:
            _login(sock, password)
            sock.sendall(b"LNK" + _pad(callsign) + struct.pack("<i", reconnect) + _pad(reflector))
            resp = _recv(sock)
            return _ack_result(resp, f"Linked to {reflector.strip().upper()}")
        except OSError as e:
            return False, str(e)
        finally:
            _close(sock)

    def unlink(self, host, port, password, callsign, reflector, protocol=0):
        """Unlink `callsign` from `reflector` for the given PROTOCOL_NAMES
        index (which protocol client to drop -- DExtra/DPlus/DCS/CCS).
        Returns (ok, message)."""
        try:
            sock = _open(host, port)
        except OSError as e:
            return False, f"Can't reach ircDDBGateway at {host}:{port} ({e})"
        try:
            _login(sock, password)
            sock.sendall(b"UNL" + _pad(callsign) + struct.pack("<i", protocol) + _pad(reflector))
            resp = _recv(sock)
            return _ack_result(resp, f"Unlinked from {reflector.strip().upper()}")
        except OSError as e:
            return False, str(e)
        finally:
            _close(sock)

    def test(self, host, port, password, callsign):
        """Settings 'Test connection' button -- login + one status read."""
        info = self.status(host, port, password, callsign)
        if info is None:
            return False, "Login failed or no response (check host/port/password/callsign)"
        rfl = info.get("reflector")
        return True, "Connected" + (f" -- linked to {rfl}" if rfl else " -- not linked")
