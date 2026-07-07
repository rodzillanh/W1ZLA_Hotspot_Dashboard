"""SSH polling and MMDVM log parsing for the hotspot fleet."""
import re
import time
import threading
import concurrent.futures
from dataclasses import asdict

import paramiko

import config
from models import HotspotStatus
from storage import load_hotspots, load_favorites, favorites_set, load_settings
from qrz import QrzClient
from radioid import RadioIdClient
from aprs import AprsClient
from brandmeister import BrandmeisterClient
import storage_activity


class FleetMonitor:
    """Holds live status for every hotspot and keeps it updated via SSH polling."""

    def __init__(self, qrz_client: QrzClient | None = None):
        self._data:     dict[str, HotspotStatus] = {}
        self._failures: dict[str, int]           = {}
        self._lock      = threading.Lock()
        self._qrz = qrz_client or QrzClient(
            config.QRZ_USERNAME, config.QRZ_PASSWORD, config.QRZ_AGENT
        )
        self._radioid       = RadioIdClient()
        self._radioid_on    = True
        self._aprs          = AprsClient("")
        self._brandmeister  = BrandmeisterClient()

    # --- public API ---

    def snapshot(self) -> dict:
        with self._lock:
            return {ip: asdict(status) for ip, status in self._data.items()}

    def set_qrz_client(self, client: QrzClient) -> None:
        """Hot-swap the QRZ client — called when credentials are saved in Settings."""
        with self._lock:
            self._qrz = client

    def set_radioid_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._radioid_on = enabled

    def set_aprs_client(self, client: AprsClient) -> None:
        """Hot-swap the APRS.fi client — called when the API key is saved in Settings."""
        with self._lock:
            self._aprs = client

    def remove(self, ip: str) -> None:
        with self._lock:
            self._data.pop(ip, None)
            self._failures.pop(ip, None)

    def map_data(self) -> dict:
        """Location data for the live map: your own hotspots (if lat/lon is
        configured for them) plus active callers + recent history.
        Only entries with known lat/lon are included."""
        nodes  = []
        for hs in load_hotspots():
            lat, lon = hs.get("lat"), hs.get("lon")
            if lat is not None and lon is not None:
                nodes.append({
                    "name": hs["name"],
                    "ip":   hs["ip"],
                    "lat":  lat,
                    "lon":  lon,
                })

        callers = []
        seen    = set()
        with self._lock:
            for status in self._data.values():
                # Active caller
                if (status.active_call and
                        status.caller_lat is not None and
                        status.caller_lon is not None and
                        status.active_call not in seen):
                    seen.add(status.active_call)
                    callers.append({
                        "call":      status.active_call,
                        "name":      status.caller_name,
                        "location":  status.caller_location,
                        "lat":       status.caller_lat,
                        "lon":       status.caller_lon,
                        "source":    status.caller_source,
                        "is_active": True,
                        "node":      status.name,
                        "node_ip":   status.ip,
                    })
                # History entries (already carry QRZ/RadioID/APRS data from
                # when they were active)
                for h in status.history:
                    call = h.get("call")
                    if call and h.get("lat") and call not in seen:
                        seen.add(call)
                        callers.append({
                            "call":      call,
                            "name":      h.get("name"),
                            "location":  h.get("location"),
                            "lat":       h["lat"],
                            "lon":       h["lon"],
                            "source":    h.get("source"),
                            "is_active": False,
                            "node":      status.name,
                            "node_ip":   status.ip,
                        })
        return {"nodes": nodes, "callers": callers}

    def run_forever(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            while True:
                list(executor.map(self.check_one, load_hotspots()))
                time.sleep(config.POLL_INTERVAL)

    def run_slow_checks_forever(self) -> None:
        """Brandmeister profile + dashboard update checks. These involve an
        extra network round-trip (Brandmeister's API, or a git ls-remote run
        on the hotspot itself) so they run on a much slower cadence than the
        main 5-second poll loop -- see config.VERSION_CHECK_INTERVAL."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            while True:
                list(executor.map(self.check_one_slow, load_hotspots()))
                time.sleep(config.VERSION_CHECK_INTERVAL)

    def check_one_slow(self, hotspot: dict) -> None:
        ip = hotspot["ip"]
        self._ensure_entry(hotspot)

        bm_id = hotspot.get("brandmeister_id")
        if bm_id:
            bm_info = self._brandmeister.lookup(bm_id)
            if bm_info is not None:
                with self._lock:
                    if ip in self._data:
                        self._data[ip].bm_status_text = bm_info["status_text"]
                        self._data[ip].bm_static_tgs   = bm_info["static_talkgroups"]

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                ip, username=hotspot["user"], password=hotspot["pass"],
                timeout=config.SSH_TIMEOUT,
            )
            _, stdout, _ = client.exec_command(config.VERSION_CHECK_CMD, timeout=config.SSH_TIMEOUT * 3)
            output = stdout.read().decode("utf-8", errors="ignore").strip()
            if output:
                webcode_version = webcode_date = None
                any_update      = False
                outdated        = []
                for line in output.splitlines():
                    if "|" not in line:
                        continue
                    name, local, remote, date = (line.split("|", 3) + ["", "", "", ""])[:4]
                    is_outdated = bool(local) and bool(remote) and local != remote
                    if is_outdated:
                        any_update = True
                        outdated.append(name)
                    if name == "WPSD-WebCode":
                        webcode_version = local or None
                        webcode_date    = date or None
                with self._lock:
                    if ip in self._data:
                        status = self._data[ip]
                        status.dashboard_version         = webcode_version
                        status.dashboard_version_date    = webcode_date
                        status.dashboard_update_available = any_update
                        status.dashboard_outdated_repos   = outdated
        except Exception:
            pass  # slow checks are best-effort -- never affect the main poll loop
        finally:
            client.close()

    # --- per-hotspot check ---

    def check_one(self, hotspot: dict) -> None:
        ip = hotspot["ip"]
        self._ensure_entry(hotspot)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                ip,
                username=hotspot["user"],
                password=hotspot["pass"],
                timeout=config.SSH_TIMEOUT,
            )
            _, stdout, _ = client.exec_command(config.SSH_STATUS_CMD, timeout=config.SSH_TIMEOUT)
            output = stdout.read().decode("utf-8", errors="ignore").splitlines()

            updates = self._parse_output(ip, output)
            with self._lock:
                status = self._data[ip]
                for field_name, value in updates.items():
                    setattr(status, field_name, value)
                self._failures[ip] = 0
        except Exception:
            with self._lock:
                self._failures[ip] += 1
                if self._failures[ip] >= config.FAILURE_THRESHOLD:
                    self._data[ip].status = "Offline"
        finally:
            client.close()

    def _ensure_entry(self, hotspot: dict) -> None:
        ip = hotspot["ip"]
        with self._lock:
            if ip not in self._data:
                self._data[ip] = HotspotStatus(name=hotspot["name"], ip=ip)
                self._failures[ip] = 0

    def _log_activity(self, ip: str) -> None:
        """Record one completed transmission for the Fleet activity metrics
        card -- opt-in (see settings.show_fleet_activity), so skip the write
        entirely when nobody will ever query it."""
        if not load_settings().get("show_fleet_activity", False):
            return
        with self._lock:
            status = self._data.get(ip)
            if status is None:
                return
            name, mode = status.name, status.mode
        storage_activity.log_activity(ip, name, mode)

    def _lookup_caller(self, call: str) -> dict:
        """Compose caller info from QRZ, RadioID.net (name/location fallback),
        and APRS.fi (live position override). Always returns a dict — missing
        fields are None rather than the whole lookup being None, so callers
        don't need extra guards."""
        with self._lock:
            qrz, radioid_on, aprs = self._qrz, self._radioid_on, self._aprs

        qrz_info = qrz.lookup(call)
        name     = qrz_info["name"]      if qrz_info else None
        location = qrz_info["location"]  if qrz_info else None
        image    = qrz_info["image_url"] if qrz_info else None
        lat      = qrz_info["lat"]       if qrz_info else None
        lon      = qrz_info["lon"]       if qrz_info else None
        source   = "qrz" if qrz_info else None

        # RadioID.net fallback — free, no auth. Only fills gaps QRZ left,
        # since QRZ's data (when available) is generally more complete.
        if radioid_on and not name:
            rid_info = self._radioid.lookup(call)
            if rid_info:
                name     = name or rid_info["name"]
                location = location or rid_info["location"]
                source   = source or "radioid"

        # APRS.fi — a live beacon position is more useful than QRZ's static
        # home-station coordinates, so it takes priority for lat/lon (but
        # never overrides name/location, which APRS doesn't provide).
        if aprs.enabled:
            aprs_info = aprs.lookup(call)
            if aprs_info:
                lat, lon = aprs_info["lat"], aprs_info["lon"]
                source   = "aprs"

        return {"name": name, "location": location, "image_url": image,
                "lat": lat, "lon": lon, "source": source}

    # --- log parsing ---

    def _parse_output(self, ip: str, output: list[str]) -> dict:
        if len(output) < 2:
            return {}

        updates = {
            "status":      "Online",
            "temperature": self._parse_temp(output[0]),
            "uptime":      output[1].replace("up ", ""),
            "cpu":         self._parse_cpu(output[2]) if len(output) > 2 else "N/A",
        }

        with self._lock:
            current   = self._data[ip]
            prev_call = current.active_call
            history   = list(current.history)

        log_lines = output[3:]

        # Color code — appears in MMDVM startup I: lines, not per-transmission.
        # Scan the whole tail for it and update whenever found (it's static so
        # this is idempotent and cheap).
        cc = self._extract_color_code(log_lines)
        if cc:
            updates["color_code"] = cc

        # Last-heard TTL: if a previous call is sitting in "last heard" state
        # and has aged past LAST_HEARD_TTL, wipe caller info now so the card
        # returns to true idle before we even parse the log lines.
        if config.LAST_HEARD_TTL > 0:
            with self._lock:
                lh = self._data[ip].last_heard
            if lh is not None and (time.time() - lh) > config.LAST_HEARD_TTL:
                updates.update({
                    "active_call":     None,
                    "caller_name":     None,
                    "caller_location": None,
                    "caller_image":    None,
                    "caller_lat":      None,
                    "caller_lon":      None,
                    "caller_source":   None,
                    "timeslot":        None,
                    "is_favorite":     False,
                    "favorite_label":  None,
                    "last_heard":      None,
                })

        # The old top-level staleness check looked at the timestamp of ANY log
        # line -- including MMDVM's periodic keepalive / network-status lines that
        # get written even when completely idle. Those lines kept the "newest
        # timestamp" perpetually fresh, so the timeout never fired.
        #
        # New approach (two-part):
        #
        # Part 1 — "call line" check: when we find a line that names a callsign,
        # we check the timestamp of THAT SPECIFIC LINE. If it's older than
        # ACTIVE_TIMEOUT, the header has gone stale and the call is over, even if
        # we never saw an explicit "end of transmission" line.
        #
        # Part 2 — "long call" keepalive: for transmissions longer than 50 log
        # lines (the header has scrolled out of the tail), there won't be a
        # callsign-bearing line in the window. In that case we check the timestamp
        # of the most recent RSSI/BER line -- those only appear during an active
        # transmission, not in periodic keepalives. If they're recent, the call is
        # still genuinely in progress.

        for line in reversed(log_lines):
            self._apply_line_metrics(line, updates)

            if self._is_end_of_transmission(line):
                # Only record last_heard timestamp the FIRST time we see this
                # end-of-transmission line. The line stays in the log tail for
                # subsequent polls so without this guard the timer resets every
                # poll cycle.
                with self._lock:
                    existing_lh = self._data[ip].last_heard
                if existing_lh is None:
                    self._log_activity(ip)
                updates.update({
                    "is_active":  False,
                    "tx_start":   None,
                    "last_heard": existing_lh if existing_lh is not None else time.time(),
                })
                return updates

            call = self._extract_call(line)
            if call:
                # ── Part 1: timestamp of the call-bearing line itself ──
                if config.ACTIVE_TIMEOUT > 0:
                    call_ts = self._extract_log_timestamp(line)
                    if call_ts is not None and (time.time() - call_ts) > config.ACTIVE_TIMEOUT:
                        # Header is stale — call ended without a clean end-of-tx marker.
                        # Enter last-heard state (keep caller info, set last_heard once)
                        # rather than jumping straight to idle. Same guard as the
                        # end-of-transmission path: only set last_heard the first time.
                        with self._lock:
                            existing_lh = self._data[ip].last_heard
                        if existing_lh is None:
                            self._log_activity(ip)
                        updates.update({
                            "is_active":  False,
                            "tx_start":   None,
                            "last_heard": existing_lh if existing_lh is not None else time.time(),
                        })
                        return updates

                if prev_call != call:
                    caller_info = self._lookup_caller(call)
                    favs      = favorites_set()
                    fav_entry = next((f for f in load_favorites() if f["call"].upper() == call), None)
                    updates.update({
                        "is_active":       True,
                        "active_call":     call,
                        "caller_name":     caller_info["name"],
                        "caller_location": caller_info["location"],
                        "caller_image":    caller_info["image_url"],
                        "caller_lat":      caller_info["lat"],
                        "caller_lon":      caller_info["lon"],
                        "caller_source":   caller_info["source"],
                        "tx_start":        time.time(),
                        "last_heard":      None,
                        "is_favorite":     call in favs,
                        "favorite_label":  fav_entry["label"] if fav_entry else None,
                        "timeslot":        self._extract_slot(line),
                    })
                    destination = self._extract_destination(line)
                    if destination:
                        updates["talkgroup"] = destination
                    if not any(h.get("call") == call for h in history):
                        history.insert(0, {
                            "call":     call,
                            "name":     caller_info["name"],
                            "location": caller_info["location"],
                            "lat":      caller_info["lat"],
                            "lon":      caller_info["lon"],
                            "source":   caller_info["source"],
                        })
                        updates["history"] = history[:config.MAX_HISTORY]
                else:
                    # Same caller still transmitting OR same caller re-keyed.
                    # Two cases require a tx_start reset:
                    #   1. tx_start is None — call went through last-heard state
                    #      and the safety net restores it.
                    #   2. last_heard is set — card was in last-heard state,
                    #      meaning the caller re-keyed. Reset tx_start even if
                    #      it still has the old value (quick re-key before
                    #      staleness check cleared it).
                    # In both cases also clear last_heard so the card exits
                    # the last-heard state immediately.
                    updates["is_active"] = True
                    with self._lock:
                        current_tx    = self._data[ip].tx_start
                        current_lh    = self._data[ip].last_heard
                    if current_tx is None or current_lh is not None:
                        # This is a re-key — reset the timer
                        updates["tx_start"]   = time.time()
                        updates["last_heard"] = None
                return updates

        # ── Part 2: header scrolled out of the 50-line tail (long transmission) ──
        # RSSI/BER lines only appear during an active call, not in keepalives.
        # If they're recent enough, the call is still going; if they're stale
        # (or absent), it's over.
        if prev_call and config.ACTIVE_TIMEOUT > 0:
            activity_ts = self._newest_activity_timestamp(log_lines)
            if activity_ts is not None and (time.time() - activity_ts) <= config.ACTIVE_TIMEOUT:
                updates["is_active"] = True
                with self._lock:
                    current_tx = self._data[ip].tx_start
                    current_lh = self._data[ip].last_heard
                if current_tx is None or current_lh is not None:
                    updates["tx_start"]   = time.time()
                    updates["last_heard"] = None
                return updates

        # Fall-through: no transmission start or end found in the log window
        # (covers long calls where the header scrolled out of the tail AND
        # no RSSI/BER lines were found). Enter last-heard state once.
        with self._lock:
            existing_lh = self._data[ip].last_heard
        updates["is_active"] = False
        updates["tx_start"]  = None
        if prev_call and existing_lh is None:
            updates["last_heard"] = time.time()
            self._log_activity(ip)
        return updates

    # --- static helpers ---

    @staticmethod
    def _extract_color_code(lines: list[str]) -> str | None:
        """Scan log lines for the MMDVM startup color code entry.
        Matches both 'Color Code: 15' (US) and 'Colour Code: 15' (UK spelling)."""
        for line in lines:
            m = re.search(config.COLOR_CODE_PATTERN, line)
            if m:
                return "CC" + m.group(1)
        return None

    @staticmethod
    def _extract_slot(line: str) -> str | None:
        """Extract timeslot number from a DMR log line, e.g. 'DMR Slot 2' -> 'TS2'."""
        m = re.search(config.SLOT_PATTERN, line)
        return "TS" + m.group(1) if m else None

    @staticmethod
    def _newest_activity_timestamp(lines: list[str]) -> float | None:
        """Return the timestamp of the most recent RSSI or BER line.

        These lines only appear during an active transmission -- never in
        MMDVM's periodic keepalive or network-status output. Using them as
        the activity signal avoids the problem where keepalive lines with
        fresh timestamps make the node look perpetually active.
        """
        for line in reversed(lines):
            if "rssi:" in line.lower() or "ber:" in line.lower():
                ts = FleetMonitor._extract_log_timestamp(line)
                if ts is not None:
                    return ts
        return None

    @staticmethod
    def _extract_log_timestamp(line: str) -> float | None:
        import datetime
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?', line)
        if not m:
            return None
        try:
            dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except ValueError:
            return None

    @staticmethod
    def _parse_temp(raw: str) -> str:
        if raw.replace(".", "", 1).isdigit():
            return f"{int(raw) / 1000.0:.1f}°C"
        return "N/A"

    @staticmethod
    def _parse_cpu(raw: str) -> str:
        """Parse CPU percentage from the awk output line, e.g. '14.3' -> '14.3%'."""
        raw = raw.strip()
        try:
            val = float(raw)
            return f"{val:.1f}%"
        except ValueError:
            return "N/A"

    @staticmethod
    def _apply_line_metrics(line: str, updates: dict) -> None:
        ber  = re.search(config.BER_PATTERN,  line)
        rssi = re.search(config.RSSI_PATTERN, line)
        mode = re.search(config.MODE_PATTERN, line)
        if ber:  updates["ber"]  = ber.group(1)  + "%"
        if rssi: updates["rssi"] = rssi.group(1) + " dBm"
        if mode: updates["mode"] = mode.group(0)

    @staticmethod
    def _is_end_of_transmission(line: str) -> bool:
        ll = line.lower()
        return any(m in ll for m in config.END_OF_TRANSMISSION_MARKERS)

    @staticmethod
    def _extract_destination(line: str) -> str | None:
        ll = line.lower()
        if " from " not in ll or " to " not in ll:
            return None
        after_from = ll.split(" from ", 1)[1]
        if " to " not in after_from:
            return None
        raw = after_from.split(" to ", 1)[1].split(",")[0].strip()
        return raw.upper() or None

    @staticmethod
    def _extract_call(line: str) -> str | None:
        ll = line.lower()
        if " from " not in ll:
            return None

        # P25 and NXDN both use "received [RF|network] transmission from CALL"
        # without a "header" keyword. Generalize the check across all modes:
        # any line with "received...transmission from" that isn't an end-of-tx
        # line is treated as a call start.
        is_network_mode_start = (
            "received" in ll and
            "transmission from" in ll and
            "end of" not in ll
        )

        if not is_network_mode_start and not any(m in ll for m in config.TRANSMISSION_START_MARKERS):
            return None

        try:
            call_raw = ll.split(" from ")[1].strip().split()[0].upper()
            call     = call_raw.replace(",", "").replace(":", "")
            if "/" in call:
                call = call.split("/")[0]
            # Suppress numeric-only IDs (e.g. P25 network subscriber ID "10999")
            # — these are DMR/P25 radio IDs, not callsigns.
            if not call or call == "NETWORK" or call.isdigit():
                return None
            return call
        except IndexError:
            pass
        return None
