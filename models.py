"""Data shapes shared across the app."""
from dataclasses import dataclass, field, asdict
from typing import Optional, List


@dataclass
class HotspotStatus:
    """Live status of one hotspot, as shown on the dashboard."""

    name: str
    ip: str
    status: str = "Connecting..."
    offline_since: Optional[float] = None  # epoch when status first became "Offline"; None if online/unknown
    # epoch of the last SUCCESSFUL poll (wpsd/asl3 only -- set alongside the
    # offline_since-clearing/failure-counter-reset logic in monitor.py's
    # _check_one_wpsd/_check_one_asl3). Stays None for openSPOT4, which is
    # push-based over its own WebSocket rather than polled -- the card
    # drawer's health line falls back to `status` alone for that type.
    last_poll_at: Optional[float] = None
    uptime: str = "Unknown"
    temperature: str = "N/A"
    cpu: str = "N/A"
    is_active: bool = False
    active_call: Optional[str] = None
    caller_name: Optional[str] = None
    caller_location: Optional[str] = None
    caller_image: Optional[str] = None
    caller_lat: Optional[float] = None
    caller_lon: Optional[float] = None
    # Which source supplied the caller's position/name -- "qrz", "radioid", or "aprs".
    # Shown on the map popup so it's clear whether a pin is a live APRS beacon
    # or a static QRZ/RadioID home-station location.
    caller_source: Optional[str] = None
    # Combined APRS symbol-table + symbol-code char (e.g. "/>" for a car),
    # only ever set when caller_source == "aprs" -- see aprs.py's lookup().
    # None for every other source, since QRZ/RadioID don't carry a symbol.
    caller_symbol: Optional[str] = None
    # tx_start: Unix epoch when the current transmission began.
    # The dashboard JS computes elapsed time from this.
    tx_start: Optional[float] = None
    last_heard: Optional[float] = None  # epoch when last transmission ended; None = never / idle
    talkgroup: Optional[str] = None
    color_code: Optional[str] = None   # static per hotspot, from MMDVM startup log lines
    timeslot: Optional[str] = None     # per-transmission, only shown when active
    is_favorite: bool = False
    favorite_label: Optional[str] = None
    # history is now a list of dicts so each entry carries its own QRZ data
    # for the live map, rather than just a callsign string.
    # Keys: call, name, location, lat, lon
    history: List[dict] = field(default_factory=list)
    ber: str = "N/A"
    rssi: str = "N/A"
    mode: str = "N/A"
    # WPSD reads this from /etc/mmdvmhost (a slow check -- see
    # config.HOTSPOT_INFO_CHECK_CMD). ASL3 reads it from /etc/sa818.conf
    # instead -- the SA818 RF module's own last-programmed frequency, on
    # the same slow cadence (see monitor.py's _check_asl3_sa818 /
    # config.SA818_CONF_CMD) -- reusing this same field rather than a
    # separate one so every existing consumer (card identity line, drawer
    # subline, etc.) picks it up with no changes. Both already formatted
    # for display; stays "N/A" for openSPOT4 (no equivalent), and for an
    # ASL3 node whose SA818 config isn't a known real frequency yet --
    # see sa818_status below for why.
    frequency: str = "N/A"  # "433.750 MHz", or "433.750/434.350 MHz" if RX != TX (duplex)
    duplex: str = "N/A"  # "Simplex" or "Duplex"
    # ASL3-only: why `frequency` above is or isn't set, sourced from the
    # SAME /etc/sa818.conf read. "recorded" (a real, non-zero frequency),
    # "placeholder" (sa818-menu wrote the file but its 000.0000 skeleton
    # was never overwritten -- confirmed live, this is what a freshly
    # imaged node looks like), "not_recorded" (no /etc/sa818.conf on this
    # host at all -- not an SA818-based node, or sa818-menu never run),
    # or None (not ASL3, or not checked yet). The SA818 module itself
    # can't be read back over the air -- this is only ever a record of
    # what THIS host last wrote, same caveat sa818-menu itself carries.
    # Sticky across polls like DVSwitch's dmr_linked/dstar_status above --
    # an SSH hiccup on this 30-min cadence shouldn't blank a known-good
    # frequency for half an hour.
    sa818_status: Optional[str] = None
    # CTCSS/DCS tone, from the SAME /etc/sa818.conf read -- only set when
    # sa818_status == "recorded" AND CURRENT_TONE is genuinely "CTCSS" or
    # "DCS" (not "None"/blank/anything else). Pre-formatted for display,
    # same convention as frequency above: "CTCSS 110.9" (RX/TX combined
    # when equal, the overwhelmingly common case) or "CTCSS RX .../TX
    # ..." if they differ; likewise "DCS <code>". None whenever no tone
    # is configured, sa818_status isn't "recorded", or not ASL3.
    sa818_tone: Optional[str] = None
    # The hotspot's own registered callsign/DMR ID (e.g. "W1ZLA (3100486)")
    # -- distinct from active_call, which is whoever's currently keying up
    # THROUGH this hotspot, not the hotspot's own identity.
    hotspot_callsign: str = "N/A"
    # The hotspot's own configured location string (e.g. "Barrington, NH")
    # -- distinct from caller_location (the active caller's QRZ-looked-up
    # location) and from this hotspot's lat/lon map coordinates (a separate
    # Settings field, not read from the device at all).
    hotspot_location: str = "N/A"
    # Brandmeister repeater profile (only populated if a brandmeister_id is
    # configured for this hotspot in Settings -> Hotspots). status_text is
    # shown as-is (e.g. "DMO") rather than collapsed into an online/offline
    # boolean, since Brandmeister's numeric status codes aren't documented.
    bm_status_text: Optional[str] = None
    bm_static_tgs: List[dict] = field(default_factory=list)  # [{talkgroup, slot}, ...]
    # WPSD/Pi-Star dashboard update check (checked on a slow background
    # cadence, not every poll -- see config.VERSION_CHECK_INTERVAL).
    # Checks all three WPSD repos (WebCode/Scripts/Binaries); dashboard_version
    # and dashboard_version_date reflect WPSD-WebCode specifically (the one
    # that actually serves this dashboard's web content), but
    # dashboard_update_available is true if ANY of the three repos is
    # behind its remote -- matching what WPSD's own Admin->Update page
    # considers, rather than only the WebCode repo.
    dashboard_version: Optional[str] = None       # local git short hash (WPSD-WebCode)
    dashboard_version_date: Optional[str] = None  # date of that commit
    dashboard_update_available: Optional[bool] = None  # None = unknown / not a git checkout
    dashboard_outdated_repos: List[str] = field(default_factory=list)  # e.g. ["WPSD-Scripts"]
    # AllStarLink (ASL3) nodes -- only populated for hotspots configured with
    # type "asl3" (see monitor.py's _check_one_asl3). active_call/is_active/
    # tx_start/last_heard/mode above are reused as-is: active_call is the
    # resolved callsign (or bare node number) of whichever linked node is
    # currently keyed, is_active is true if any link is keyed.
    asl_node: Optional[str] = None  # this node's own ASL node number
    # Link topology -- who's connected to this node right now, from
    # `asterisk -rx "rpt xnode <node>"`'s RPT_ALINKS variable. Distinct from
    # `history` (the talker log), which WPSD/Pi-Star also has -- this has no
    # WPSD equivalent at all.
    # Keys: node, callsign/description/location (None if not resolved via
    # aslstats.py), mode ("T"/"R"/"L"/"C"), keyed (bool)
    asl_linked_nodes: List[dict] = field(default_factory=list)
    # DVSwitch (Analog_Bridge) card -- only populated for ASL3 hotspots with
    # dvswitch_enabled set (see monitor.py's _check_one_asl3/_check_dvswitch_tx).
    # dvswitch_bridges: one entry per configured port, {port, tuned, mode,
    # use_fallback} -- tuned/mode/use_fallback all come from that
    # instance's own /tmp/ABInfo_<port>.json, re-read fresh every poll.
    # "tuned" is "TG <n>" from that JSON's digital.tg field when present
    # (confirmed live -- matches the exact dst= value seen in a real Begin
    # TX line for the same bridge), falling back to the top-level
    # last_tune field otherwise (confirmed live to be "" -- not useful --
    # for a static-TG bridge; only relevant for a dynamic-retuning setup
    # this app hasn't seen a real example of). None if neither is present.
    # use_fallback is True/False/None from that same JSON's own
    # use_fallback field (confirmed live, cross-checked against the log
    # lines below at the time it was found) -- the PRIMARY source for
    # dvswitch_vocoder now, since it's re-read every poll and can't go
    # stale the way the log-based signal below does.
    # dvswitch_vocoder is "software"/"hardware"/None (unknown -- no signal
    # from either source yet), preferring the live use_fallback above
    # (aggregated across configured bridges: any bridge on fallback marks
    # the whole card "software") and falling back to a one-time
    # Analog_Bridge.log startup line ("Using software MBE decoder..." /
    # "Using hardware AMBE vocoder") only when no bridge has a usable
    # use_fallback value -- that log line is written once per process
    # start and silently ages out once the log rotates, which is why it's
    # no longer the primary source. dvswitch_heard mirrors `history`'s shape
    # but WITH a real
    # timestamp per entry (unlike `history`, which has none) -- {call,
    # dmr_id, dst, seen_at}, call falls back to the bare DMR ID when the
    # log line's own call= field is absent.
    dvswitch_bridges: List[dict] = field(default_factory=list)
    dvswitch_vocoder: Optional[str] = None
    dvswitch_heard: List[dict] = field(default_factory=list)
    # Live RX/TX state + link status, sourced from a DIFFERENT DVSwitch
    # component's log than everything above (MMDVM_Bridge.log, not
    # Analog_Bridge.log) -- confirmed real end-of-transmission lines with
    # actual duration/loss/BER, unlike Analog_Bridge.log's start-only
    # signal. Scoped to DMR + D-Star only (YSF/P25/NXDN write to their own
    # separate gateway logs, not covered). dvswitch_live is None when
    # idle, else {mode, call, target} for whichever DMR/D-Star
    # transmission is currently open (re-scanned fresh from the log tail
    # every poll, not tracked across polls -- same "no persistent state"
    # shape as everything else this app tails). dvswitch_dmr_linked is
    # True/False/None (unknown -- no DMR master connect/disconnect line
    # seen yet); dvswitch_dstar_status is the raw quoted string from
    # D-Star's own explicit "link status set to ..." line, or None.
    dvswitch_live: Optional[dict] = None
    dvswitch_dmr_linked: Optional[bool] = None
    dvswitch_dstar_status: Optional[str] = None
    # openSPOT4 battery -- only ever populated for type=="openspot4" hotspots
    # running on battery power, sourced from a "pwr: batt ..." line in the
    # SAME "log" WebSocket messages _MODE_LOG_RE already opportunistically
    # reads a mode name from (see openspot.py's _BATTERY_LOG_RE / _on_log).
    # Not in the documented HTTP API at all -- confirmed live, this only
    # ever shows up in the live log stream. None for a mains-powered unit,
    # which simply never emits this line.
    battery_pct: Optional[int] = None
    battery_mv: Optional[int] = None
    battery_est_min: Optional[int] = None  # device's own estimated minutes remaining
    battery_usb_ma: Optional[int] = None   # USB input current
    battery_charge_ma: Optional[int] = None
    battery_charging: Optional[bool] = None  # charge_ma > 0
    battery_cpu_temp_c: Optional[float] = None  # device board temp, from the same "pwr:" line
    # openSPOT4's own periodic "net-chk: ok (N ms)" round-trip check, from
    # the SAME "log" WebSocket messages battery is sourced from (see
    # openspot.py's _NET_CHK_LOG_RE). net_check_ms is None whenever
    # net_check_ok is False (or unknown) -- a failed check has no
    # meaningful latency to show.
    net_check_ok: Optional[bool] = None
    net_check_ms: Optional[int] = None
    # From the structured "pwr" WebSocket message only (openspot.py's
    # _on_pwr) -- the "pwr: batt ..." log-line scrape has no equivalent of
    # these three, so they stay None until/unless that structured message
    # has actually been seen for this device.
    battery_detected: Optional[bool] = None
    battery_fault: Optional[bool] = None
    battery_low_curr: Optional[bool] = None  # underpowered USB input warning
    # WiFi signal -- only ever set for a unit actually connected over WiFi
    # (openspot.py's _on_wifirssi); stays None for a wired/Ethernet unit.
    # wifi_ap_client's exact meaning is unconfirmed (see openspot.py) --
    # kept as a raw passthrough, not interpreted into anything more
    # specific.
    wifi_rssi_dbm: Optional[int] = None
    wifi_ap_client: Optional[int] = None
    # openSPOT4's currently active config profile (Brandmeister/TGIF/YSF/
    # ...), resolved from an unsolicited "resp" message the device pushes
    # right after the WebSocket opens (openspot.py's _on_resp) -- confirmed
    # to match the device's own admin UI's "Active config profile: N
    # (Name)" display. active_config_profile_num is 1-indexed, matching
    # that UI convention (not the raw 0-indexed active_cp value).
    active_config_profile_num: Optional[int] = None
    active_config_profile_name: Optional[str] = None
    # openSPOT4's PERSISTENT connector-level link (which reflector/room/
    # master it's connected to right now, independent of an active call)
    # -- from an unsolicited "connectedto" WS message (openspot.py's
    # _on_connectedto). Deliberately separate from `talkgroup` (call-
    # scoped, set only during an active call).
    connector_target: Optional[str] = None   # e.g. "YSF 32592"
    connector_server: Optional[str] = None   # e.g. "americalink.radiotechnology.xyz"
    # Which WiFi network wifi_rssi_dbm is actually measuring -- from a
    # "netstate" WS message (openspot.py's _on_netstate). None for a
    # wired/Ethernet unit, same as wifi_rssi_dbm.
    wifi_ssid: Optional[str] = None
    # openSPOT4's OWN built-in APRS-IS connection/messaging (the "APRS
    # chat" feature on its own admin Status page) -- entirely separate
    # from this app's own aprs_inbox.py/aprs_messaging.py, which maintain
    # their OWN independent APRS-IS session under settings.json's own
    # credentials. aprs_conn_state comes from an "aprsbgstate" WS message
    # (confirmed live across a real connect cycle: "disconnected"/
    # "connecting"/"connected" -- other raw values unseen, dropped rather
    # than guessed at). aprs_messages is a bounded recent-activity list
    # (openspot.py's _on_aprsmsg/_on_aprstxmsgwaitack/_on_aprstxmsggotack),
    # each entry {direction: "in"/"out", callsign, text, ts, acked} --
    # acked is only meaningful for outbound entries (None for inbound).
    aprs_conn_state: Optional[str] = None
    aprs_messages: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
