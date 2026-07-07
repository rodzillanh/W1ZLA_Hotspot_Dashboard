"""Data shapes shared across the app."""
from dataclasses import dataclass, field, asdict
from typing import Optional, List


@dataclass
class HotspotStatus:
    """Live status of one hotspot, as shown on the dashboard."""

    name: str
    ip: str
    status: str = "Connecting..."
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

    def to_dict(self) -> dict:
        return asdict(self)
