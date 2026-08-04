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
    # WPSD-only (read from /etc/mmdvmhost, a slow check -- see
    # config.HOTSPOT_INFO_CHECK_CMD). All already formatted for display;
    # all stay "N/A" for ASL3/openSPOT4, which have no equivalent.
    frequency: str = "N/A"  # "433.750 MHz", or "433.750/434.350 MHz" if RX != TX (duplex)
    duplex: str = "N/A"  # "Simplex" or "Duplex"
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
    # dvswitch_bridges: one entry per configured port, {port, tuned, mode} --
    # tuned/mode come from that instance's own /tmp/ABInfo_<port>.json.
    # "tuned" is "TG <n>" from that JSON's digital.tg field when present
    # (confirmed live -- matches the exact dst= value seen in a real Begin
    # TX line for the same bridge), falling back to the top-level
    # last_tune field otherwise (confirmed live to be "" -- not useful --
    # for a static-TG bridge; only relevant for a dynamic-retuning setup
    # this app hasn't seen a real example of). None if neither is present.
    # dvswitch_vocoder is "software"/"hardware"/
    # None (unknown -- no DVSwitch log output seen at all yet), based on a
    # real confirmed log line ("Using software MBE decoder...") rather than
    # config alone. dvswitch_heard mirrors `history`'s shape but WITH a real
    # timestamp per entry (unlike `history`, which has none) -- {call,
    # dmr_id, dst, seen_at}, call falls back to the bare DMR ID when the
    # log line's own call= field is absent.
    dvswitch_bridges: List[dict] = field(default_factory=list)
    dvswitch_vocoder: Optional[str] = None
    dvswitch_heard: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
