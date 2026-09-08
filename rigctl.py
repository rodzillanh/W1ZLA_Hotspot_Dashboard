"""Hamlib rigctld client -- tap-to-tune a real HF rig from the POTA card.

rigctld is ham radio's universal "CAT control over TCP" server: WFView
has one built in (Settings -> enable RigCtld), and standalone `rigctld`
(Hamlib) or apps like SparkSDR / Thetis expose the same thing. The wire
protocol is line-based ASCII, default TCP port 4532:

    F 14261000\\n   -> set VFO frequency (Hz)          -> "RPRT 0\\n" on success
    M USB 0\\n       -> set mode + passband (0=default)  -> "RPRT 0\\n"
    f\\n             -> get frequency                    -> "<hz>\\n"
    _\\n             -> get_info                         -> "<rig model>\\n"

`RPRT 0` = success, `RPRT -<n>` = error (from a `set`). A `get` replies
with the value itself and only emits `RPRT` on failure.

Same contract as every other integration module here: self-contained,
never raises out of its public methods (returns `(False, reason)` / a
status dict on any failure), no persistent connection. Every call opens
a short-lived socket, sends one or two lines, reads the reply, closes.
rigctld serializes CAT access, so this never collides with WSJT-X / a
logger holding their own connections -- unlike POTACAT ECHOCAT, which
bumps an older client when a new one connects.

No auth -- rigctld has none. Same trusted-LAN assumption as the hotspot
web-UI links elsewhere in this app.
"""
import socket
import threading
import time

DEFAULT_PORT = 4532
TIMEOUT = 4.0
STATUS_TTL = 20  # seconds -- the POTA card polls /api/pota every 20s;
                 # no reason to open a probe socket more often than that.


def _split_host(host, port):
    """Accept ("host", 4532) or a bare "host:4532" pasted into the host
    field -- an explicit "host:port" always wins, since that's
    unambiguous intent."""
    host = (host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if host.count(":") == 1:
        h, _, p = host.partition(":")
        if p.strip().isdigit():
            return h.strip(), int(p)
    return host, port


def pota_mode_to_rig(pota_mode, freq_hz):
    """POTA's spot feed uses loose mode labels ("SSB", "CW", "FT8",
    "DATA", "DYNAMIC"...). Map to what rigctld's `M` command wants, or
    None to leave the rig's current mode alone."""
    m = (pota_mode or "").strip().upper()
    if not m:
        return None
    if m in ("USB", "LSB", "CW", "CWR", "AM", "FM", "RTTY",
             "PKTUSB", "PKTLSB", "PKTFM", "USB-D", "LSB-D"):
        return {"USB-D": "PKTUSB", "LSB-D": "PKTLSB"}.get(m, m)
    if m == "SSB":
        # No USB/LSB in the spot -- universal HF convention: LSB below
        # 10 MHz, USB at/above.
        return "USB" if (freq_hz or 0) >= 10_000_000 else "LSB"
    if m in ("FT8", "FT4", "JS8", "PSK", "PSK31", "MFSK", "OLIVIA",
             "DIGITAL", "DIGITALVOICE", "DATA", "DYNAMIC"):
        return "PKTUSB"
    return None


class RigctlClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._status_cache = {}  # (host, port) -> (expiry_epoch, result dict)

    # --- public ---

    def status(self, host, port=DEFAULT_PORT, force=False):
        """{"reachable": bool, "rig": str|None, "freq_hz": int|None,
        "error": str|None} -- cached STATUS_TTL. Drives the POTA card's
        rig pill and whether the tune chips render at all."""
        h, p = _split_host(host, port)
        if not h:
            return {"reachable": False, "rig": None, "freq_hz": None,
                    "error": "No rig server host configured"}
        key = (h, p)
        now = time.time()
        with self._lock:
            hit = self._status_cache.get(key)
            if hit and not force and hit[0] > now:
                return hit[1]
        result = self._probe(h, p)
        with self._lock:
            self._status_cache[key] = (now + STATUS_TTL, result)
        return result

    def tune(self, host, port, freq_hz, pota_mode=""):
        """Set the rig's frequency (and, if pota_mode maps to something,
        its mode). Returns (ok, human message). Never raises."""
        h, p = _split_host(host, port)
        if not h:
            return False, "No rig server host configured"
        try:
            freq_hz = int(freq_hz)
        except (TypeError, ValueError):
            return False, "Bad frequency"
        if freq_hz <= 0:
            return False, "Bad frequency"
        rig_mode = pota_mode_to_rig(pota_mode, freq_hz)
        try:
            with socket.create_connection((h, p), timeout=TIMEOUT) as sock:
                sock.settimeout(TIMEOUT)
                ok, reply = self._cmd(sock, f"F {freq_hz}")
                if not ok:
                    return False, f"rigctld rejected the frequency ({reply})"
                mode_note = ""
                if rig_mode:
                    mok, mreply = self._cmd(sock, f"M {rig_mode} 0")
                    mode_note = f" {rig_mode}" if mok else f" (mode set failed: {mreply})"
        except OSError as e:
            return False, f"Can't reach rig server at {h}:{p} ({e})"
        # A successful tune is the freshest possible status -- refresh the
        # cache so the card's pill reflects it on the next poll.
        with self._lock:
            self._status_cache[(h, p)] = (time.time() + STATUS_TTL, {
                "reachable": True, "rig": None, "freq_hz": freq_hz, "error": None,
            })
        return True, f"{freq_hz / 1e6:.3f} MHz{mode_note}"

    def test(self, host, port=DEFAULT_PORT):
        """Settings 'Test connection' button -- forces a fresh probe."""
        r = self.status(host, port, force=True)
        if not r["reachable"]:
            return False, r.get("error") or "No response from rig server"
        bits = []
        if r.get("rig"):
            bits.append(f'rig "{r["rig"]}"')
        if r.get("freq_hz"):
            bits.append(f'{r["freq_hz"] / 1e6:.3f} MHz')
        return True, "rigctld reachable" + (" -- " + " · ".join(bits) if bits else "")

    # --- internals ---

    @staticmethod
    def _cmd(sock, line):
        """Send one command line, read one reply line. `set` commands
        reply 'RPRT 0' / 'RPRT -N'; `get` commands reply with the value
        (no RPRT unless it errored). Returns (ok, reply text)."""
        try:
            sock.sendall((line + "\n").encode())
            raw = RigctlClient._readline(sock)
        except OSError as e:
            return False, str(e)
        reply = raw.strip()
        if reply.startswith("RPRT"):
            return reply[4:].strip() == "0", reply
        return True, reply  # a value line from a `get`

    @staticmethod
    def _readline(sock):
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace")

    def _probe(self, host, port):
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
                sock.settimeout(TIMEOUT)
                _, freq_reply = self._cmd(sock, "f")
                freq_hz = None
                try:
                    freq_hz = int(float(freq_reply.split()[0]))
                except (ValueError, IndexError):
                    pass  # e.g. rigctld in --vfo mode wants a VFO arg --
                          # still proves the server is reachable
                rig = None
                try:
                    ok, info = self._cmd(sock, "_")
                    if ok and info:
                        rig = info.split(":", 1)[-1].strip() or None
                except OSError:
                    pass
                return {"reachable": True, "rig": rig, "freq_hz": freq_hz, "error": None}
        except OSError as e:
            return {"reachable": False, "rig": None, "freq_hz": None,
                    "error": f"{host}:{port} unreachable ({e})"}
