"""SSH polling and MMDVM log parsing for the hotspot fleet."""
import re
import time
import threading
import concurrent.futures
from collections import deque
from dataclasses import asdict

import paramiko

import config
from models import HotspotStatus
from storage import load_hotspots, load_favorites, favorites_set, load_settings
from qrz import QrzClient
from radioid import RadioIdClient
from aprs import AprsClient
from brandmeister import BrandmeisterClient
from aslstats import AslStatsClient
import storage_activity
import storage_notifications


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
        self._aslstats      = AslStatsClient()
        # Last-seen DVSwitch "Begin TX:" line per hotspot ip -- there's no
        # confirmed end-of-transmission line for DVSwitch (see
        # config.DVSWITCH_BEGIN_TX_PATTERN), so a Fleet Activity row is
        # logged when this changes (a new start seen), not on completion
        # the way every other mode is. In-memory only, same as
        # self._failures -- losing this on restart just means the next
        # poll's first "Begin TX" line (even if actually already logged
        # before restart) gets counted once more, a one-time, cosmetic
        # over-count, not worth persisting for.
        self._dvswitch_last_tx: dict[str, str] = {}
        # Fleet online/offline transition log for the Notifications card --
        # NOT a new poll/connection, just an event appended at the exact
        # moments _record_failure()/the success paths below already flip
        # status.offline_since between None and a timestamp. Bounded deque,
        # same in-memory-only pattern as aprs_inbox.py's _messages -- these
        # don't need to survive a restart.
        self._fleet_events: deque = deque(maxlen=100)
        # Reseed from disk so a restart doesn't lose this history -- fleet
        # events append oldest-to-newest (unlike aprs/hamalert's
        # appendleft), so replaying the oldest-first persisted list
        # through the same .append() reproduces live insertion order.
        for payload in storage_notifications.recent("fleet", self._fleet_events.maxlen):
            self._fleet_events.append(payload)

    # --- public API ---

    def snapshot(self) -> dict:
        with self._lock:
            return {ip: asdict(status) for ip, status in self._data.items()}

    def fleet_events(self) -> list:
        """Recent online/offline transitions, newest first, for the
        Notifications card. Call sites append under self._lock already
        held (see _record_failure/_check_one_wpsd/_check_one_asl3/
        apply_external_update) -- this just returns a snapshot copy."""
        with self._lock:
            return list(reversed(self._fleet_events))

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

    def prune_stale(self, current_ips: set) -> None:
        """Drop any monitor entries for IPs no longer in the hotspot
        config. Called at the top of each poll cycle as a safety net --
        an in-flight check_one/check_one_slow worker thread for a
        just-deleted hotspot can call _ensure_entry() and recreate its
        entry in self._data right after remove() ran for it."""
        with self._lock:
            stale = [ip for ip in self._data if ip not in current_ips]
            for ip in stale:
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
                # A disabled hotspot stays in hotspots.json (config/creds
                # retained) but is excluded from both polling and
                # prune_stale's keep-set, so it's neither checked nor shown
                # on the dashboard until re-enabled.
                hotspots = [h for h in load_hotspots() if h.get("enabled", True)]
                self.prune_stale({h["ip"] for h in hotspots})
                list(executor.map(self.check_one, hotspots))
                time.sleep(config.POLL_INTERVAL)

    def run_slow_checks_forever(self) -> None:
        """Brandmeister profile + dashboard update checks. These involve an
        extra network round-trip (Brandmeister's API, or a git ls-remote run
        on the hotspot itself) so they run on a much slower cadence than the
        main 5-second poll loop -- see config.VERSION_CHECK_INTERVAL."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            while True:
                hotspots = [h for h in load_hotspots() if h.get("enabled", True)]
                self.prune_stale({h["ip"] for h in hotspots})
                list(executor.map(self.check_one_slow, hotspots))
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

        # WPSD/Pi-Star-specific -- ASL3 nodes have no git-based dashboard
        # checkout to compare against a remote, so this whole check would
        # just be a wasted SSH round-trip (and could print meaningless
        # check_repo output if those hardcoded paths happen to exist for
        # unrelated reasons on an ASL3 image).
        if hotspot.get("type", "wpsd") != "wpsd":
            return

        try:
            output = self._ssh_exec(hotspot, config.VERSION_CHECK_CMD, config.SSH_TIMEOUT * 3).strip()
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

        try:
            info_output = self._ssh_exec(hotspot, config.HOTSPOT_INFO_CHECK_CMD, config.SSH_TIMEOUT).strip()
            rx_mhz = tx_mhz = None
            duplex = callsign = dmr_id = location = None
            for line in info_output.splitlines():
                key, sep, val = line.partition("=")
                if not sep:
                    continue
                val = val.strip().strip('"')
                if key == "RXFrequency" and val.isdigit():
                    rx_mhz = int(val) / 1_000_000
                elif key == "TXFrequency" and val.isdigit():
                    tx_mhz = int(val) / 1_000_000
                elif key == "Duplex":
                    duplex = {"0": "Simplex", "1": "Duplex"}.get(val)
                elif key == "Callsign" and val:
                    callsign = val
                elif key == "Id" and val and val != "0":
                    dmr_id = val
                elif key == "Location" and val:
                    location = val

            updates = {}
            if rx_mhz is not None:
                # Simplex (the overwhelming majority of WPSD hotspots) shows
                # one number, matching WPSD's own dashboard's "TX/RX Freq."
                # display; only shows both if they genuinely differ (duplex).
                updates["frequency"] = (
                    f"{rx_mhz:.3f} MHz" if tx_mhz is None or abs(tx_mhz - rx_mhz) < 0.0001
                    else f"{rx_mhz:.3f}/{tx_mhz:.3f} MHz"
                )
            if duplex is not None:
                updates["duplex"] = duplex
            if callsign:
                updates["hotspot_callsign"] = f"{callsign} ({dmr_id})" if dmr_id else callsign
            if location:
                updates["hotspot_location"] = location

            if updates:
                with self._lock:
                    if ip in self._data:
                        status = self._data[ip]
                        for field_name, value in updates.items():
                            setattr(status, field_name, value)
        except Exception:
            pass  # best-effort, same as the update check above

    # --- per-hotspot check ---

    def check_one(self, hotspot: dict) -> None:
        """Dispatch to the right poll/parse path for this hotspot's type --
        "wpsd" (MMDVM log tail, default for backward compat with existing
        hotspots.json entries that predate this field), "asl3"
        (AllStarLink, `rpt xnode`), or "openspot4" (SharkRF openSPOT4,
        pushed via openspot.py's persistent WebSocket worker instead of
        polled here)."""
        self._ensure_entry(hotspot)
        node_type = hotspot.get("type", "wpsd")
        if node_type == "asl3":
            self._check_one_asl3(hotspot)
        elif node_type == "openspot4":
            self._check_one_openspot4(hotspot)
        else:
            self._check_one_wpsd(hotspot)

    def _check_one_wpsd(self, hotspot: dict) -> None:
        ip = hotspot["ip"]
        try:
            output = self._ssh_exec(hotspot, config.SSH_STATUS_CMD, config.SSH_TIMEOUT).splitlines()
            updates = self._parse_output(ip, output)
            with self._lock:
                status = self._data[ip]
                for field_name, value in updates.items():
                    setattr(status, field_name, value)
                self._failures[ip] = 0
                if status.offline_since is not None:
                    self._record_fleet_event(ip, status.name, "online")
                status.offline_since = None
        except Exception:
            self._record_failure(ip)

    def _check_one_asl3(self, hotspot: dict) -> None:
        ip       = hotspot["ip"]
        node     = hotspot.get("asl_node", "")
        dvswitch = hotspot.get("dvswitch_enabled", False)
        try:
            cmd = config.build_asl_status_cmd(node, dvswitch_enabled=dvswitch)
            output = self._ssh_exec(hotspot, cmd, config.SSH_TIMEOUT).splitlines()
            updates = self._parse_asl_output(ip, node, output)
            with self._lock:
                status = self._data[ip]
                for field_name, value in updates.items():
                    setattr(status, field_name, value)
                self._failures[ip] = 0
                if status.offline_since is not None:
                    self._record_fleet_event(ip, status.name, "online")
                status.offline_since = None
            if dvswitch:
                self._check_dvswitch_tx(ip, output)
        except Exception:
            self._record_failure(ip)

    def _check_dvswitch_tx(self, ip: str, output: list[str]) -> None:
        """Scans the DVSwitch (Analog_Bridge) log tail appended onto this
        hotspot's SSH output (see config.build_asl_status_cmd's
        dvswitch_enabled param) for the one confirmed-real DVSwitch log
        line, "Begin TX: ...". There's no confirmed end-of-transmission
        line for DVSwitch the way every other mode has one (see
        config.DVSWITCH_BEGIN_TX_PATTERN's own comment) -- so this logs one
        Fleet Activity row per NEWLY SEEN start line (different from the
        last one recorded for this hotspot), not per completed
        transmission. A real, disclosed difference in what's being
        counted for this mode specifically, not a hidden shortcut."""
        last_tx_line = None
        for line in output:
            if re.search(config.DVSWITCH_BEGIN_TX_PATTERN, line):
                last_tx_line = line.strip()
        if last_tx_line is None:
            return
        with self._lock:
            if self._dvswitch_last_tx.get(ip) == last_tx_line:
                return
            self._dvswitch_last_tx[ip] = last_tx_line
            name = self._data[ip].name
        storage_activity.log_activity(ip, name, "DVSwitch")

    def _check_one_openspot4(self, hotspot: dict) -> None:
        """openspot.py's persistent WebSocket worker pushes live field
        updates via apply_external_update() -- there's no SSH poll to run
        here. This tick's only job is the same LAST_HEARD_TTL aging check
        WPSD/ASL3 run every cycle (_apply_last_heard_ttl is pure
        state-based logic, not triggered by a log line), since without
        it a last-heard caller would never clear on an openspot4 card."""
        ip = hotspot["ip"]
        updates: dict = {}
        self._apply_last_heard_ttl(ip, updates)
        if updates:
            with self._lock:
                status = self._data[ip]
                for field_name, value in updates.items():
                    setattr(status, field_name, value)

    def apply_external_update(self, ip: str, updates: dict) -> None:
        """Push-based integrations (currently just openspot.py) merge a
        batch of field updates under the shared lock. Receiving ANY
        message over a persistent connection is itself proof of liveness
        -- the WS analogue of a successful SSH poll -- so this also
        resets the failure counter and clears offline_since. Drops the
        update (rather than KeyError-ing) if ip isn't in self._data yet --
        a possible race between the openspot worker thread starting and
        check_one()'s first _ensure_entry() tick at app startup; the next
        5s poll tick creates the entry and the next WS message lands
        fine."""
        with self._lock:
            if ip not in self._data:
                return
            status = self._data[ip]
            for field_name, value in updates.items():
                setattr(status, field_name, value)
            self._failures[ip] = 0
            status.status = "Online"
            if status.offline_since is not None:
                self._record_fleet_event(ip, status.name, "online")
            status.offline_since = None

    def mark_external_offline(self, ip: str) -> None:
        """Called by openspot.py when a reconnect attempt fails -- same
        FAILURE_THRESHOLD-gated Offline transition as _record_failure(),
        just driven by connection state instead of a per-poll exception."""
        self._record_failure(ip)

    def lookup_caller_info(self, call: str) -> dict:
        """Public wrapper around _lookup_caller for push-based
        integrations that don't go through check_one()'s SSH poll path."""
        return self._lookup_caller(call)

    def apply_favorite_match(self, call: str) -> "tuple[bool, str | None]":
        return self._apply_favorite_match(call)

    def log_activity(self, ip: str) -> None:
        self._log_activity(ip)

    def _ssh_exec(self, hotspot: dict, cmd: str, timeout: int) -> str:
        """Connect, run one command, return its decoded stdout. Raises on
        any connection/exec failure -- callers handle via _record_failure()."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hotspot["ip"], username=hotspot["user"], password=hotspot["pass"],
                timeout=config.SSH_TIMEOUT,
            )
            _, stdout, _ = client.exec_command(cmd, timeout=timeout)
            return stdout.read().decode("utf-8", errors="ignore")
        finally:
            client.close()

    def _record_fleet_event(self, ip: str, name: str, kind: str) -> None:
        """Appends one online/offline transition event. Caller must
        already hold self._lock -- this is a plain (non-reentrant)
        threading.Lock, so this method never acquires it itself.

        Persists to storage_notifications too, so a restart doesn't lose
        this history -- called inline here (briefly under self._lock)
        rather than restructuring the 4 call sites to persist after
        releasing it, same "infrequent event, cheap enough to do inline"
        reasoning storage_activity.log_activity() already uses for its
        own inline prune. log_notification() never raises, so this can't
        turn a transient DB hiccup into a held/corrupted fleet lock."""
        event = {"ip": ip, "name": name, "kind": kind, "at": time.time()}
        self._fleet_events.append(event)
        storage_notifications.log_notification("fleet", event["at"], event, self._fleet_events.maxlen)

    def _record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures[ip] = self._failures.get(ip, 0) + 1
            # ip may not be in self._data yet -- previously safe to assume
            # since this was only ever called after _ensure_entry() ran
            # earlier in the same check_one() invocation, but
            # mark_external_offline() (openspot.py) can call in from a
            # separate thread before that first tick lands.
            if ip in self._data and self._failures[ip] >= config.FAILURE_THRESHOLD:
                status = self._data[ip]
                status.status = "Offline"
                if status.offline_since is None:
                    status.offline_since = time.time()
                    self._record_fleet_event(ip, status.name, "offline")

    def _ensure_entry(self, hotspot: dict) -> None:
        ip = hotspot["ip"]
        with self._lock:
            if ip not in self._data:
                self._data[ip] = HotspotStatus(name=hotspot["name"], ip=ip)
                self._failures[ip] = 0
            else:
                # Keep the name in sync with hotspots.json -- a rename in
                # Settings doesn't change the ip, so without this the live
                # HotspotStatus (created once, above) would keep showing
                # the old name until the app restarts.
                self._data[ip].name = hotspot["name"]

    def _log_activity(self, ip: str) -> None:
        """Record one completed transmission for the Fleet activity metrics
        card -- opt-in (see settings.show_fleet_activity), so skip the write
        entirely when nobody will ever query it.

        Also captures target/target_type for the Top 5 activity card --
        WHO transmitted (active_call), not which talkgroup/node it went
        through. active_call already holds the right value for both
        hotspot types: for WPSD it's the caller's own resolved callsign;
        for ASL3 it's the keyed linked node's resolved callsign, or its
        bare node number when no callsign could be resolved (still a
        meaningful ranking identity, just not a real callsign). A
        hotspot type with neither (e.g. openspot4, which doesn't
        currently call this) just logs target=None, invisible to
        top_targets().

        `via` is the WPSD talkgroup this specific transmission was heard
        on, shown alongside the callsign for context (not used for
        ranking) -- ASL3 has no separate concept here (status.talkgroup
        is never set for it; the linked node captured in active_call
        already IS the "channel", so there's nothing distinct to add)."""
        if not load_settings().get("show_fleet_activity", False):
            return
        with self._lock:
            status = self._data.get(ip)
            if status is None:
                return
            name, mode = status.name, status.mode
            target = status.active_call or None
            target_type = "callsign" if target else None
            via = status.talkgroup or None
        storage_activity.log_activity(ip, name, mode, target, target_type, via)

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
        # city/state/country: QRZ-only (RadioID has no equivalent fields) --
        # kept separate from "location" for the Recent Contacts card, which
        # wants a country flag and city/state as distinct fields.
        city     = qrz_info["city"]    if qrz_info else None
        state    = qrz_info["state"]   if qrz_info else None
        country  = qrz_info["country"] if qrz_info else None

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
                "lat": lat, "lon": lon, "source": source,
                "city": city, "state": state, "country": country}

    @staticmethod
    def _apply_favorite_match(call: str) -> tuple[bool, str | None]:
        """Shared by both the WPSD and ASL3 parse paths -- pure lookup
        against the configured favorites list, nothing WPSD-specific."""
        favs      = favorites_set()
        fav_entry = next((f for f in load_favorites() if f["call"].upper() == call), None)
        return call in favs, (fav_entry["label"] if fav_entry else None)

    def _apply_last_heard_ttl(self, ip: str, updates: dict) -> None:
        """If a previous call is sitting in "last heard" state and has aged
        past LAST_HEARD_TTL, wipe caller info so the card returns to true
        idle. Pure state-based logic (no log parsing involved), shared by
        both the WPSD and ASL3 paths."""
        if config.LAST_HEARD_TTL <= 0:
            return
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

        # Last-heard TTL: wipe caller info now so the card returns to true
        # idle before we even parse the log lines, if it's aged out.
        self._apply_last_heard_ttl(ip, updates)

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
                    is_favorite, favorite_label = self._apply_favorite_match(call)
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
                        "is_favorite":     is_favorite,
                        "favorite_label":  favorite_label,
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

    def _parse_asl_output(self, ip: str, node: str, output: list[str]) -> dict:
        """Parse an ASL3 (AllStarLink) hotspot's SSH output: the same
        generic Linux temp/uptime/CPU one-liners as WPSD, plus `rpt xnode`'s
        dialplan-variable dump -- RPT_ALINKS gives real per-linked-node
        keyed state (confirmed against a real node; see
        config.build_asl_status_cmd). Unlike the WPSD log tail, each poll
        is a complete, current snapshot of link state rather than a rolling
        window, so this only needs to compare against the previous poll's
        result -- no "header scrolled out of the tail" case to handle."""
        if len(output) < 2:
            return {}

        updates = {
            "status":      "Online",
            "temperature": self._parse_temp(output[0]),
            "uptime":      output[1].replace("up ", ""),
            "cpu":         self._parse_cpu(output[2]) if len(output) > 2 else "N/A",
            "asl_node":    node,
            "mode":        "ASL",
        }

        self._apply_last_heard_ttl(ip, updates)

        with self._lock:
            current   = self._data[ip]
            prev_call = current.active_call
            history   = list(current.history)

        alinks_raw = None
        for line in output[3:]:
            m = re.match(config.ASL_ALINKS_LINE_PATTERN, line.strip())
            if m:
                alinks_raw = m.group(1)
                break

        linked_nodes = []
        keyed_entry  = None
        if alinks_raw:
            node_info = self._aslstats.linked_node_info(node)
            for entry in alinks_raw.split(",")[1:]:  # first field is the count
                em = re.match(config.ASL_ALINK_ENTRY_PATTERN, entry.strip())
                if not em:
                    continue
                link_node, mode_char, key_char = em.groups()
                info = node_info.get(link_node) or {}
                entry_dict = {
                    "node":        link_node,
                    "callsign":    info.get("callsign"),
                    "description": info.get("description"),
                    "location":    info.get("location"),
                    "mode":        mode_char,
                    "keyed":       key_char == "K",
                }
                linked_nodes.append(entry_dict)
                if entry_dict["keyed"] and keyed_entry is None:
                    keyed_entry = entry_dict
        updates["asl_linked_nodes"] = linked_nodes

        if keyed_entry:
            call              = keyed_entry["callsign"] or keyed_entry["node"]
            has_real_callsign = keyed_entry["callsign"] is not None

            if prev_call != call:
                # Only run the QRZ/RadioID/APRS enrichment lookups when we
                # actually resolved a callsign -- a bare node number isn't
                # one, and looking it up would just waste an API call.
                caller_info = self._lookup_caller(call) if has_real_callsign else {
                    "name": None, "location": None, "image_url": None,
                    "lat": None, "lon": None, "source": None,
                }
                is_favorite, favorite_label = self._apply_favorite_match(call)
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
                    "is_favorite":     is_favorite,
                    "favorite_label":  favorite_label,
                })
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
                # Same node still keyed -- mirrors the WPSD "same caller"
                # branch: only reset tx_start if we were previously idle
                # or in last-heard state (i.e. this is a re-key).
                updates["is_active"] = True
                with self._lock:
                    current_tx = self._data[ip].tx_start
                    current_lh = self._data[ip].last_heard
                if current_tx is None or current_lh is not None:
                    updates["tx_start"]   = time.time()
                    updates["last_heard"] = None
            return updates

        # Nobody keyed right now -- enter last-heard state once (same guard
        # as the WPSD path: only set last_heard and log activity the first
        # time this transition is seen).
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
