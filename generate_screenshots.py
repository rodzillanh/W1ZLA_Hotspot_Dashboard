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
    # The live audio-level VU meter card row (see asl_audio.py) is driven
    # by a REAL background worker that SSHes to this fake TEST-NET-2 IP
    # and fails forever in the background -- harmless (same
    # degrade-gracefully contract every other integration here has), but
    # it means /api/audio_level never has a real level for this demo IP.
    # audio_manager.snapshot is monkeypatched below (screenshot-only,
    # same "fake a live state for illustration" trick already used for
    # the ASL Favorites "recently active" screenshot -- see CLAUDE.md)
    # so the meter actually shows a lit, moving bar in the capture.
    "audio_meter_enabled": True,
}
# A second DVSwitch-enabled ASL3 node -- purely so the redesigned DVSwitch
# card's "Show:" picker (v3.99) has a real second option in its screenshot,
# not just a dropdown with one entry that undersells the whole point of
# consolidating what used to be one card per node into one card total.
HOTSPOT_ASL3_RELAY = {
    "ip": "198.51.100.13", "name": "Northeast Relay", "type": "asl3",
    "user": "root", "pass": "demo", "enabled": True, "asl_node": "60067",
    "lat": "42.3601", "lon": "-71.0589",
    "dvswitch_enabled": True, "dvswitch_ports": "31000",
}
HOTSPOTS = [HOTSPOT_WPSD_ACTIVE, HOTSPOT_WPSD_IDLE, HOTSPOT_ASL3, HOTSPOT_ASL3_RELAY]

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
    "show_starlink_trains": True,
    "show_flights_overhead": True,
    "show_recent_contacts": True,
    "show_qso_stats": True,
    "show_top_activity": True,
    "show_pota": True,
    "pota_callsign": "W1ZLA",
    "show_hf_favorites": True,
    # Rig control on so the POTA card screenshot shows the tap-to-tune
    # frequency chips + the reachability pill -- the actual rigctld
    # probe is monkeypatched to a fixed "reachable" reply below (there's
    # no rig server in this sandbox), same screenshot-only trick as the
    # audio_manager.snapshot patch further down.
    "rig_control_enabled": True,
    "rig_host": "192.168.1.44",
}

# 7 favorites, no "pinned" key -- deliberately legacy-shaped so this run
# also exercises storage.load_asl_favorites()'s own migration (the first
# ASL_FAV_CARD_CAP in list order default to pinned) the same way a real
# upgrading install would, rather than hand-writing the post-migration
# shape. 27339/2020 stay first so they land in the pinned 5 and the tile
# grid screenshot shows a real keyed + a real connected tile, not just
# idle ones; the last two (48496/68204) are deliberately left unpinned to
# demonstrate the "+N more" overflow hint.
ASL_FAVORITES = [
    {"node": "27339", "label": "East Coast AllStar HUB"},
    {"node": "2020", "label": "TAC-2 National"},
    {"node": "31700", "label": "New England Link"},
    {"node": "51000", "label": "Granite State Repeater"},
    {"node": "60672", "label": "W2ECR Hub"},
    {"node": "48496", "label": "Northeast Repeater Group"},
    {"node": "68204", "label": "Southern Maine ARC"},
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
        # confirmed via the QRZ Logbook sync -- shows the "✓ QRZ" badge on
        # the Recent Contacts card / the confirmation line in its drawer
        "qrz_logid": 918273, "qrz_confirmed": True,
        "qrz_confirmed_at": time.time() - 1800,
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

# monitor._data is keyed by each hotspot's stable id, not ip (two hotspots
# can share an ip -- see storage.load_hotspots()'s id backfill). The dicts
# above were written to hotspots.json before that backfill ever ran, so
# re-read the now-backfilled file to learn each demo hotspot's real id.
_ip_to_id = {h["ip"]: h["id"] for h in app.load_hotspots()}

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
    # SA818 card (v4.14/v4.15) -- real values from this exact node's own
    # /etc/sa818.conf, not fabricated.
    sa818_status="recorded", frequency="443.5000 MHz", duplex="Simplex",
    sa818_tone="CTCSS 110.9",
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
# Idle -- deliberately quiet (collapsed one-line DVSwitch state, no linked
# nodes) so the DVSwitch card's "Show:" picker screenshot has a genuinely
# different second option to switch to, not a copy of the first.
asl3_relay = models.HotspotStatus(
    name=HOTSPOT_ASL3_RELAY["name"], ip=HOTSPOT_ASL3_RELAY["ip"],
    status="Online", last_poll_at=now, uptime="6d 11h 40m",
    asl_node="60067", asl_linked_nodes=[],
    dvswitch_bridges=[{"port": "31000", "tuned": None, "mode": None, "use_fallback": None}],
    dvswitch_vocoder="software",
)

with app.monitor._lock:
    for hs in (wpsd_active, wpsd_idle, asl3, asl3_relay):
        hs.id = _ip_to_id[hs.ip]
        app.monitor._data[hs.id] = hs

# Fake a live, mid-level reading for the audio meter's fast /api/audio_level
# poll -- the real AslAudioManager.reconcile() (called eagerly by app.py at
# import time, same as openspot_manager) DOES start a real worker for
# HOTSPOT_ASL3's audio_meter_enabled=True, but it can only ever fail against
# this fake TEST-NET-2 IP. Screenshot-only monkeypatch, never touches real
# behavior -- same trick already used for the ASL Favorites "recently
# active" state screenshot (see CLAUDE.md). Keyed by id, not ip, matching
# AslAudioManager.snapshot()'s real shape.
app.audio_manager.snapshot = lambda: {_ip_to_id[HOTSPOT_ASL3["ip"]]: {"level_dbfs": -26.0, "connected": True}}

# Same idea for the POTA card's rig-control pill / tap-to-tune chips:
# /api/pota calls rig_client.status(), which can only ever fail against a
# fake host in this sandbox. Pin it to a "reachable" reply so the chips
# render enabled and the pill shows a rig model. Screenshot-only.
app.rig_client.status = lambda *a, **k: {
    "reachable": True, "rig": "IC-7300", "freq_hz": 14074000, "error": None,
}

# --- 4. Seed Fleet Activity / Top 5 Activity (storage_activity.py) --
# log_activity() always timestamps "now", which is fine for a demo chart.
# Durations are deliberately NOT proportional to call count -- W2ECR's one
# long QSO outranks KC1ABC's three short check-ins under the Top 5 card's
# new default "talk time" ranking, same non-monotonic-by-design dataset
# used to validate that ranking (see chat/commit history), so the
# screenshot actually demonstrates why duration-ranking exists rather than
# just looking like a re-skinned count.
for call, mode, tg, duration in [
    ("KC1ABC", "DMR", "31665", 34), ("W2ECR", "DMR", "31665", 245),
    ("N1LCP", "DMR", "3172", 58), ("KC1ABC", "DMR", "31665", 27),
    ("W4KEV", "D-Star", None, 112),
]:
    storage_activity.log_activity(
        HOTSPOT_WPSD_ACTIVE["ip"], HOTSPOT_WPSD_ACTIVE["name"], mode,
        target=call, target_type="callsign", via=tg, duration=duration,
        hotspot_id=_ip_to_id[HOTSPOT_WPSD_ACTIVE["ip"]],
    )
# DVSwitch card's own footer sparkline reads the same activity_log table,
# filtered to mode="DVSwitch" -- see storage_activity.dvswitch_sparkline().
# Real DVSwitch begin-tx events never carry a duration (no confirmed
# end-of-transmission line -- see CLAUDE.md), but this demo passes short
# ones anyway so these rows don't render as a literal "0s" in the Top 5
# card's now-default talk-time view.
for call, duration in (("W1ZLA", 14), ("KC1ABC", 9), ("W1ZLA", 11), ("N1LCP", 6)):
    storage_activity.log_activity(
        HOTSPOT_ASL3["ip"], HOTSPOT_ASL3["name"], "DVSwitch",
        target=call, target_type="callsign", duration=duration,
        hotspot_id=_ip_to_id[HOTSPOT_ASL3["ip"]],
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
    # dashboard.html builds each hotspot card's id as "card-<hotspot id>"
    # (the stable hs-... id, NOT the ip -- changed by the hotspot-id
    # migration); _ip_to_id maps the demo ip back to whatever id the
    # backfill assigned it this run.
    (f"card-{_ip_to_id[HOTSPOT_WPSD_ACTIVE['ip']]}", "hotspot-card-wpsd-active.png"),
    (f"card-{_ip_to_id[HOTSPOT_WPSD_IDLE['ip']]}", "hotspot-card-wpsd-idle.png"),
    (f"card-{_ip_to_id[HOTSPOT_ASL3['ip']]}", "hotspot-card-asl3.png"),
    ("dvswitch-card", "dvswitch-card.png"),  # one consolidated card since v3.99, not one per node
    ("fleet-activity-card", "fleet-activity-card.png"),
    ("asl-favorites-card", "asl-favorites-card.png"),
    ("satellites-card", "satellites-card.png"),
    ("flights-card", "flights-card.png"),
    ("recent-contacts-card", "recent-contacts-card.png"),
    ("qso-stats-card", "qso-stats-card.png"),
    ("top-activity-card", "top-activity-card.png"),
    ("hf-conditions-card", "hf-conditions-card.png"),
    ("band-plan-card", "band-plan-card.png"),
    ("license-quiz-card", "license-quiz-card.png"),
    ("wspr-activity-card", "band-activity-card.png"),
    ("big-clock-card", "big-clock-card.png"),
    ("pota-card", "pota-card.png"),
    ("hf-favorites-card", "hf-favorites-card.png"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    # "networkidle" no longer works for this page as of the audio meter
    # feature (v4.32) -- fetchAudioLevels() polls /api/audio_level every
    # ~250ms for as long as the page is open, so the page NEVER goes
    # 500ms without a request, and "networkidle" waits forever. "load"
    # plus the explicit wait_for_timeout below (already relied on to let
    # the poll cycle + card JS populate) is what actually matters here.
    page.goto(BASE_URL, wait_until="load")
    page.wait_for_timeout(2500)  # let the poll cycle + card JS populate
    # The Starlink train sub-section (inside satellites-card) does two
    # SEQUENTIAL live fetches (a CelesTrak page scrape, then a TLE fetch)
    # before it has anything to show -- give it a bit longer specifically,
    # rather than raising the wait above for every card (that pushed the
    # capture loop closer to the dashboard's own 3s poll cycle re-rendering
    # #cards-grid mid-loop, which detached an element handle mid-screenshot).
    page.wait_for_timeout(2000)

    page.screenshot(path=os.path.join(OUTPUT_DIR, "dashboard-full.png"), full_page=True)
    print("wrote dashboard-full.png")

    for element_id, filename in CARD_SHOTS:
        # [id="..."] attribute-equality form, not "#id" -- kept from when
        # hotspot card ids embedded a dotted fake IP that a bare "#"
        # selector misparsed ("Unexpected token '.51'"); harmless now that
        # ids are "card-hs-...", and still the safer form regardless.
        #
        # Retried once on a real, observed race: the dashboard's own 3s
        # poll cycle can replace #cards-grid's innerHTML between querying
        # the element and Playwright's screenshot() actually firing,
        # detaching the handle mid-capture ("Element is not attached to
        # the DOM") -- re-querying a moment later picks up the freshly
        # rendered element instead of retrying against the stale handle.
        for attempt in range(2):
            el = page.query_selector(f'[id="{element_id}"]')
            if not el or not el.is_visible():
                print(f"skip (not found/visible): #{element_id}")
                break
            try:
                el.screenshot(path=os.path.join(OUTPUT_DIR, filename))
                print(f"wrote {filename}")
                break
            except Exception as e:
                if attempt == 0:
                    page.wait_for_timeout(500)
                    continue
                print(f"FAILED (after retry): #{element_id}: {e}")

    page.click('a.settings-link')  # opens the Quick Settings drawer (not /setup -- that's a separate capture below)
    page.wait_for_timeout(500)
    qs_drawer = page.query_selector('[id="settings-drawer"]')
    if qs_drawer and qs_drawer.is_visible():
        qs_drawer.screenshot(path=os.path.join(OUTPUT_DIR, "quick-settings.png"))
        print("wrote quick-settings.png")
    page.click('[id="settings-drawer"] .hs-drawer-close')  # scoped -- every drawer has one of these

    page.click('.tab-btn[data-tab="map"]')
    page.wait_for_timeout(3000)  # tile load
    page.screenshot(path=os.path.join(OUTPUT_DIR, "live-map.png"))
    print("wrote live-map.png")

    page.goto(f"{BASE_URL}/setup", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.screenshot(path=os.path.join(OUTPUT_DIR, "settings.png"))
    print("wrote settings.png")

    # --- Mobile companion view (Pocket Dash, /mobile) -- a phone-sized
    # context so the single-column layout + bottom tab bar render as they
    # would on a real device. Same seeded demo state as everything above.
    mob_ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2, is_mobile=True, has_touch=True,
    )
    mob = mob_ctx.new_page()
    mob.goto(f"{BASE_URL}/mobile", wait_until="load")
    mob.wait_for_timeout(2500)  # let the first /api/data poll + card JS populate
    mob.screenshot(path=os.path.join(OUTPUT_DIR, "mobile-status.png"), full_page=True)
    print("wrote mobile-status.png")

    mob.click('.tabbar button[data-go="map"]')
    mob.wait_for_timeout(3500)  # lazy Leaflet init + tile load
    mob.screenshot(path=os.path.join(OUTPUT_DIR, "mobile-map.png"))
    print("wrote mobile-map.png")

    mob.click('.tabbar button[data-go="more"]')
    mob.wait_for_timeout(900)
    mob.screenshot(path=os.path.join(OUTPUT_DIR, "mobile-notifications.png"), full_page=True)
    print("wrote mobile-notifications.png")

    # The per-hotspot alerts screen -- opened directly. The "Customize per
    # hotspot" button that normally opens it only appears once a push
    # subscription exists, which headless Chromium can't create here.
    mob.evaluate("openPrefsScreen()")
    mob.wait_for_timeout(700)
    mob.screenshot(path=os.path.join(OUTPUT_DIR, "mobile-alert-prefs.png"), full_page=True)
    print("wrote mobile-alert-prefs.png")

    # The glanceable focus screen -- what a tapped push notification opens
    # into. Reached via the ?focus=<hotspot id> deep link the push payload
    # carries.
    _focus_id = _ip_to_id[HOTSPOT_WPSD_ACTIVE["ip"]]
    mob.goto(f"{BASE_URL}/mobile?focus={_focus_id}", wait_until="load")
    mob.wait_for_timeout(2500)
    mob.screenshot(path=os.path.join(OUTPUT_DIR, "mobile-focus.png"))
    print("wrote mobile-focus.png")

    mob_ctx.close()

    browser.close()

print(f"\nDone -- screenshots written to {OUTPUT_DIR}")
shutil.rmtree(CONFIG_DIR, ignore_errors=True)
