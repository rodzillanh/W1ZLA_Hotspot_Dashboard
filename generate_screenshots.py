#!/usr/bin/env python3
"""Generates real screenshots of the actual running dashboard, seeded with
synthetic demo data (fake hotspots/QSOs/activity -- never real fleet data),
for use as wiki illustrations. Dev-only tool; not part of the deployed app.

Usage:
    pip install -r requirements-dev.txt
    playwright install chromium   # one-time, downloads a browser binary
    python3 generate_screenshots.py

Outputs PNGs to screenshots/ (gitignored -- generated artifacts, copied into
the wiki's own images/ folder by sync-wiki.sh, not committed to this repo
directly).

How this avoids needing real hardware/network: a HotspotStatus is normally
only populated by monitor.py's real SSH polling (_check_one_wpsd/
_check_one_asl3/_check_one_openspot4) -- writing hotspots.json alone only
supplies STATIC config (ip/name/type), not live status, so there's no way
to fake an "active call" card that way. Instead this script imports app.py
directly (safe -- confirmed live that background polling threads only ever
start inside main(), never on import) and writes fake HotspotStatus objects
straight into app.monitor._data, bypassing SSH/the network entirely.
app.py's own waitress.serve() is then run in a background thread so a real
browser hits the real Flask routes/templates/JS against this seeded state.

openSPOT4 is deliberately NOT included in the demo set: app.py eagerly
calls `openspot_manager.reconcile(load_hotspots())` at import time
(confirmed by reading app.py before writing this), which would spin up a
REAL WebSocket worker trying to reach whatever fake IP a demo openSPOT4
hotspot used -- that worker's own real reconnect-with-backoff logic could
call mark_external_offline() and overwrite the seeded status before the
screenshot ever fires. WPSD/ASL3 have no such risk: both are purely
poll-based, and the only thing that ever polls them (monitor.run_forever())
is started inside main(), which this script never calls.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "screenshots")
DEMO_PORT = 5099

# --- 1. Seed a throwaway CONFIG_DIR *before* importing app.py -- storage.py's
# load_*() functions just read whatever's on disk at call time, and several
# of app.py's module-level _rebuild_*() calls (aprs_inbox/wsjtx/hamalert/
# brandmeister_lh) read settings.json immediately on import.
CONFIG_DIR = tempfile.mkdtemp(prefix="hotspot-dashboard-demo-")
os.environ["CONFIG_DIR"] = CONFIG_DIR
os.environ["PORT"] = str(DEMO_PORT)

# Fake, non-routable IPs (RFC 5737 TEST-NET-2, never assignable on a real
# network) -- these never need to actually be reachable, since live status
# is seeded directly into monitor._data below, not polled.
HOTSPOT_WPSD_ACTIVE = {
    "ip": "198.51.100.10", "name": "W1ZLA Home", "type": "wpsd",
    "user": "pi-star", "pass": "demo", "enabled": True,
    "lat": "43.2162", "lon": "-71.0132",
}
HOTSPOT_WPSD_IDLE = {
    "ip": "198.51.100.11", "name": "Mobile Hotspot", "type": "wpsd",
    "user": "pi-star", "pass": "demo", "enabled": True,
    "lat": "43.0642", "lon": "-71.3073",
}
HOTSPOT_ASL3 = {
    "ip": "198.51.100.12", "name": "W1ZLA Hub", "type": "asl3",
    "user": "root", "pass": "demo", "enabled": True, "asl_node": "59929",
    "lat": "43.2162", "lon": "-71.0132",
    "dvswitch_enabled": True, "dvswitch_ports": "31000,31001",
}
HOTSPOTS = [HOTSPOT_WPSD_ACTIVE, HOTSPOT_WPSD_IDLE, HOTSPOT_ASL3]

SETTINGS = {
    "dashboard_name": "W1ZLA Hotspot Dashboard",
    "station_grid": "FN43ig",
    "weather_location": "Barrington, NH",
    "weather_unit": "F",
    "show_host_stats": True,
    "show_toolbar": True,
    "show_fleet_activity": True,
    "show_asl_favorites": True,
    "show_hf_conditions": True,
    "show_band_plan": True,
    "show_license_quiz": True,
    "show_wspr_activity": True,
    "show_big_clock": True,
    "show_satellites": True,
    "show_recent_contacts": True,
    "show_qso_stats": True,
    "show_top_activity": True,
}

ASL_FAVORITES = [
    {"node": "27339", "label": "East Coast AllStar HUB"},
    {"node": "2020", "label": "TAC-2 National"},
    {"node": "48496", "label": "Northeast Repeater Group"},
]

QSOS = [
    {
        "call": "KC1ABC", "band": "20m", "mode": "FT8", "date": "20260805",
        "grid": "FN42aa", "lat": 43.5, "lon": -70.9,
        "name": "Mike", "city": "Portland", "state": "ME", "country": "UNITED STATES",
        "rst_sent": "-05", "rst_rcvd": "-08", "frequency_hz": 14074000,
        "logged_at": time.time() - 3600, "source": "wsjtx",
    },
    {
        "call": "G4XYZ", "band": "20m", "mode": "FT8", "date": "20260805",
        "grid": "IO91wm", "lat": 52.5, "lon": -1.9,
        "name": "James", "city": "Birmingham", "state": "", "country": "ENGLAND",
        "rst_sent": "-10", "rst_rcvd": "-12", "frequency_hz": 14074000,
        "logged_at": time.time() - 7200, "source": "wsjtx",
    },
    {
        "call": "VE3QRS", "band": "40m", "mode": "SSB", "date": "20260804",
        "grid": "FN03gs", "lat": 43.7, "lon": -79.4,
        "name": "Robert", "city": "Toronto", "state": "ON", "country": "CANADA",
        "rst_sent": "59", "rst_rcvd": "57", "frequency_hz": 7185000,
        "logged_at": time.time() - 90000,
    },
]


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


os.makedirs(CONFIG_DIR, exist_ok=True)
_write_json(os.path.join(CONFIG_DIR, "hotspots.json"), HOTSPOTS)
_write_json(os.path.join(CONFIG_DIR, "settings.json"), SETTINGS)
_write_json(os.path.join(CONFIG_DIR, "asl_favorites.json"), ASL_FAVORITES)
_write_json(os.path.join(CONFIG_DIR, "qsos.json"), QSOS)

# --- 2. Import app.py -- safe, background threads only ever start inside
# main() (confirmed by reading app.py's `if __name__ == "__main__":` block
# before relying on this), never as a side effect of import.
sys.path.insert(0, SCRIPT_DIR)
import app  # noqa: E402
import models  # noqa: E402
import storage_activity  # noqa: E402
import waitress  # noqa: E402

# --- 3. Seed live status directly into monitor._data, bypassing SSH entirely.
now = time.time()

wpsd_active = models.HotspotStatus(
    name=HOTSPOT_WPSD_ACTIVE["name"], ip=HOTSPOT_WPSD_ACTIVE["ip"],
    status="Online", last_poll_at=now, uptime="14d 6h 22m",
    temperature="118.4°F", cpu="12%",
    is_active=True, active_call="KC1ABC", caller_name="Mike Chen",
    caller_location="Portland, ME", tx_start=now - 12,
    talkgroup="31665", color_code="1", timeslot="2",
    ber="0.1%", rssi="-72 dBm / -6", mode="DMR",
    frequency="433.750 MHz", duplex="Simplex",
    hotspot_callsign="W1ZLA (3100486)", hotspot_location="Barrington, NH",
    history=[
        {"call": "KC1ABC", "name": "Mike Chen", "location": "Portland, ME"},
        {"call": "W2ECR", "name": "East Coast Hub", "location": "New York, NY"},
    ],
)
wpsd_idle = models.HotspotStatus(
    name=HOTSPOT_WPSD_IDLE["name"], ip=HOTSPOT_WPSD_IDLE["ip"],
    status="Online", last_poll_at=now, uptime="2d 1h 5m",
    temperature="104.9°F", cpu="8%",
    last_heard=now - 1800, ber="0.0%", rssi="-58 dBm / -4", mode="D-Star",
    frequency="434.100 MHz", duplex="Duplex",
    hotspot_callsign="W1ZLA (3100487)", hotspot_location="Manchester, NH",
)
asl3 = models.HotspotStatus(
    name=HOTSPOT_ASL3["name"], ip=HOTSPOT_ASL3["ip"],
    status="Online", last_poll_at=now, uptime="41d 3h 0m",
    asl_node="59929",
    asl_linked_nodes=[
        {"node": "27339", "callsign": "W2ECR", "description": "East Coast AllStar HUB",
         "location": "New York, NY", "mode": "T", "keyed": True},
        {"node": "2020", "callsign": None, "description": None,
         "location": None, "mode": "R", "keyed": False},
    ],
    is_active=True, active_call="W2ECR", tx_start=now - 6,
    # DVSwitch card demo data -- one bridge actively tuned/keyed, one idle,
    # a real (fake) transmission in progress via dvswitch_live so the
    # screenshot shows the redesigned card's active/tinted state, not just
    # its collapsed idle line.
    dvswitch_bridges=[
        {"port": "31000", "tuned": "TG 603", "mode": "AMBE+2", "use_fallback": False},
        {"port": "31001", "tuned": None, "mode": None, "use_fallback": None},
    ],
    dvswitch_vocoder="hardware",
    dvswitch_heard=[
        {"call": "W1ZLA", "dmr_id": "3100486", "dst": "603", "seen_at": now},
        {"call": "KC1ABC", "dmr_id": "3141592", "dst": "603", "seen_at": now - 240},
    ],
    dvswitch_live={"mode": "DMR", "call": "W1ZLA", "target": "TG 603"},
    dvswitch_dmr_linked=True,
)

with app.monitor._lock:
    for hs in (wpsd_active, wpsd_idle, asl3):
        app.monitor._data[hs.ip] = hs

# --- 4. Seed Fleet Activity / Top 5 Activity (storage_activity.py) --
# log_activity() always timestamps "now", which is fine for a demo chart.
for call, mode, tg in [
    ("KC1ABC", "DMR", "31665"), ("W2ECR", "DMR", "31665"),
    ("N1LCP", "DMR", "3172"), ("KC1ABC", "DMR", "31665"),
    ("W4KEV", "D-Star", None),
]:
    storage_activity.log_activity(
        HOTSPOT_WPSD_ACTIVE["ip"], HOTSPOT_WPSD_ACTIVE["name"], mode,
        target=call, target_type="callsign", via=tg,
    )
# DVSwitch card's own footer sparkline reads the same activity_log table,
# filtered to mode="DVSwitch" -- see storage_activity.dvswitch_sparkline().
for call in ("W1ZLA", "KC1ABC", "W1ZLA", "N1LCP"):
    storage_activity.log_activity(
        HOTSPOT_ASL3["ip"], HOTSPOT_ASL3["name"], "DVSwitch",
        target=call, target_type="callsign",
    )

# --- 5. Start the real app the same way main() does (waitress, not the
# Flask dev server -- see CLAUDE.md's own gotcha on why waitress specifically).
server_thread = threading.Thread(
    target=lambda: waitress.serve(app.app, host="127.0.0.1", port=DEMO_PORT, threads=8),
    daemon=True,
)
server_thread.start()
time.sleep(1.5)  # give waitress a moment to bind before Playwright connects

# --- 6. Capture screenshots with Playwright.
from playwright.sync_api import sync_playwright  # noqa: E402

os.makedirs(OUTPUT_DIR, exist_ok=True)
BASE_URL = f"http://127.0.0.1:{DEMO_PORT}"

CARD_SHOTS = [
    (f"card-{HOTSPOT_WPSD_ACTIVE['ip']}", "hotspot-card-wpsd-active.png"),
    (f"card-{HOTSPOT_WPSD_IDLE['ip']}", "hotspot-card-wpsd-idle.png"),
    (f"card-{HOTSPOT_ASL3['ip']}", "hotspot-card-asl3.png"),
    (f"dvswitch-card-{HOTSPOT_ASL3['ip']}", "dvswitch-card.png"),
    ("fleet-activity-card", "fleet-activity-card.png"),
    ("asl-favorites-card", "asl-favorites-card.png"),
    ("satellites-card", "satellites-card.png"),
    ("recent-contacts-card", "recent-contacts-card.png"),
    ("qso-stats-card", "qso-stats-card.png"),
    ("top-activity-card", "top-activity-card.png"),
    ("hf-conditions-card", "hf-conditions-card.png"),
    ("band-plan-card", "band-plan-card.png"),
    ("license-quiz-card", "license-quiz-card.png"),
    ("wspr-activity-card", "band-activity-card.png"),
    ("big-clock-card", "big-clock-card.png"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_timeout(2500)  # let the poll cycle + card JS populate

    page.screenshot(path=os.path.join(OUTPUT_DIR, "dashboard-full.png"), full_page=True)
    print("wrote dashboard-full.png")

    for element_id, filename in CARD_SHOTS:
        # [id="..."] attribute-equality form, not "#id" -- the hotspot card
        # ids embed a fake IP address (e.g. "card-198.51.100.10"), and a
        # bare "#" selector misparses the dots as class-selector separators
        # (confirmed live: Playwright raised "Unexpected token '.51'").
        el = page.query_selector(f'[id="{element_id}"]')
        if el and el.is_visible():
            el.screenshot(path=os.path.join(OUTPUT_DIR, filename))
            print(f"wrote {filename}")
        else:
            print(f"skip (not found/visible): #{element_id}")

    page.click('.tab-btn[data-tab="map"]')
    page.wait_for_timeout(3000)  # tile load
    page.screenshot(path=os.path.join(OUTPUT_DIR, "live-map.png"))
    print("wrote live-map.png")

    page.goto(f"{BASE_URL}/setup", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.screenshot(path=os.path.join(OUTPUT_DIR, "settings.png"))
    print("wrote settings.png")

    browser.close()

print(f"\nDone -- screenshots written to {OUTPUT_DIR}")
shutil.rmtree(CONFIG_DIR, ignore_errors=True)
