"""Polls a DigiPi (KM6LYW's Raspberry Pi ham radio data hotspot) over SSH
for Direwolf/APRS activity -- a dedicated card, not a hotspot type, since a
digipeater doesn't have one "active call" the way a WPSD/ASL3 repeater does
(see CLAUDE.md for why that ruled out reusing HotspotStatus).

Confirmed against a real DigiPi before writing this, not guessed from
DigiPi's own docs (which don't document the log format at all): Direwolf
isn't a systemd service on a real device, it's started by a plain shell
script and logs to /run/direwolf.log in a stable, parseable text format --
a companion script (direwatch.py, bundled with DigiPi) already reads that
exact file to drive the device's physical screen, which is what gave
confidence the format is stable enough to tail and parse here too.

Line format (three tags observed on a real device, all confirmed against
two separate captures spanning many repeats):
    [0.4] W1ZLA-10>APMI04,WIDE2-2:@192225z4312.97N/07100.79W-...
    [ig] W1ZLA-2>APDW18:!R8\\&Q<R`C&  ! DigiPi http://digipi.org/
    [ig>tx] W1ZLA-10>APMI04,TCPIP*,qAS,W1ZLA:@192230z...
[0.N] = heard directly over RF, [ig] = received from the APRS-IS internet
feed, [ig>tx] = heard on RF and gated out to the internet. Non-packet noise
lines (DCD carrier-detect toggles, audio-level diagnostics, blank lines)
don't match the packet regex below and are implicitly dropped -- no
special-case exclusion list needed.

Deliberately NOT decoding APRS position payloads (especially the
compressed format used in the digipeater's own beacon, e.g. "R8\\&Q<R`C&")
-- that's a real risk of showing wrong map data if gotten wrong, and unlike
grid_to_latlon() elsewhere in this project, it hasn't been verified against
real data. Payload text is shown as captured, not parsed into coordinates.
"""
import re
import time
import threading
from collections import deque

import paramiko

import config
from storage import load_settings

PACKET_RE = re.compile(r"^\[([\w.>]+)\]\s+([\w-]+)>([^:]+):(.*)$")

# First payload character -> cheap type label, per the APRS spec's data
# type identifier -- doesn't require decoding the rest of the payload.
_TYPE_CHARS = {
    "@": "pos", "!": "pos", "=": "pos", "/": "pos",
    ":": "msg",
    ">": "status",
    ";": "object",
    "?": "query",
    "T": "telemetry",
}


def _classify_tag(tag: str) -> str:
    if tag == "ig>tx":
        return "RF → Internet"
    if tag == "ig":
        return "Internet"
    return "RF"  # bare "N.N" channel.subchannel form


def _packet_type(payload: str) -> str:
    return _TYPE_CHARS.get(payload[:1], "other") if payload else "other"


def parse_direwolf_lines(output: str) -> list[dict]:
    """Parses raw `tail`'d direwolf.log text into packet dicts, newest last.
    Non-packet lines (DCD toggles, audio-level diagnostics, blanks) simply
    don't match PACKET_RE and are skipped -- no separate exclusion list."""
    packets = []
    for line in output.splitlines():
        m = PACKET_RE.match(line.strip())
        if not m:
            continue
        tag, call, _dest_path, payload = m.groups()
        packets.append({
            "tag":       tag,
            "direction": _classify_tag(tag),
            "call":      call,
            "type":      _packet_type(payload),
            "payload":   payload.strip(),
        })
    return packets


class DigipiMonitor:
    """Periodic SSH poll, not a persistent connection -- settings are read
    fresh every cycle (same pattern as FleetMonitor.run_forever()'s own
    `load_hotspots()` call), so a settings save just takes effect on the
    next poll with no explicit configure()/reconnect call needed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._packets = deque(maxlen=50)
        self._seen = deque(maxlen=50)  # (tag, call, payload) tuples already in _packets
        self._state = "disabled"  # disabled | connecting | connected | offline
        self._failures = 0
        self._temp = "N/A"
        self._cpu = "N/A"
        self._uptime = "Unknown"
        self._ip = ""

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._state != "disabled",
                "state":   self._state,
                "ip":      self._ip,
                "temp":    self._temp,
                "cpu":     self._cpu,
                "uptime":  self._uptime,
            }

    def packets(self) -> list:
        with self._lock:
            return list(reversed(self._packets))  # newest first

    def run_forever(self) -> None:
        while True:
            settings = load_settings()
            ip   = settings.get("digipi_ip", "").strip()
            user = settings.get("digipi_user", "").strip()
            pw   = settings.get("digipi_pass", "")
            if not settings.get("digipi_enabled", False) or not (ip and user and pw):
                with self._lock:
                    self._state = "disabled"
                time.sleep(config.POLL_INTERVAL)
                continue
            self._poll_once(ip, user, pw)
            time.sleep(config.POLL_INTERVAL)

    def _poll_once(self, ip: str, user: str, pw: str) -> None:
        with self._lock:
            self._ip = ip
            if self._state == "disabled":
                self._state = "connecting"
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(ip, username=user, password=pw, timeout=config.SSH_TIMEOUT)
            _, stdout, _ = client.exec_command(
                config.build_digipi_status_cmd(), timeout=config.SSH_TIMEOUT
            )
            output = stdout.read().decode("utf-8", errors="ignore")
        except Exception:
            with self._lock:
                self._failures += 1
                if self._failures >= config.FAILURE_THRESHOLD:
                    self._state = "offline"
            return
        finally:
            client.close()

        lines = output.splitlines()
        temp = _parse_temp(lines[0]) if len(lines) > 0 else "N/A"
        uptime = lines[1].replace("up ", "") if len(lines) > 1 else "Unknown"
        cpu = _parse_cpu(lines[2]) if len(lines) > 2 else "N/A"
        new_packets = parse_direwolf_lines("\n".join(lines[3:]))

        with self._lock:
            self._failures = 0
            self._state = "connected"
            self._temp = temp
            self._cpu = cpu
            self._uptime = uptime
            for p in new_packets:
                key = (p["tag"], p["call"], p["payload"])
                if key in self._seen:
                    continue
                self._seen.append(key)
                self._packets.append(p)


def _parse_temp(raw: str) -> str:
    """Same logic as monitor.py's FleetMonitor._parse_temp -- kept as its
    own small copy rather than imported, matching this project's
    self-contained-per-integration convention (qrz.py, hf_conditions.py,
    etc. each own their parsing rather than sharing monitor.py internals)."""
    raw = raw.strip()
    if raw.replace(".", "", 1).isdigit():
        return f"{int(raw) / 1000.0:.1f}°C"
    return "N/A"


def _parse_cpu(raw: str) -> str:
    """Same logic as monitor.py's FleetMonitor._parse_cpu."""
    raw = raw.strip()
    try:
        return f"{float(raw):.1f}%"
    except ValueError:
        return "N/A"
