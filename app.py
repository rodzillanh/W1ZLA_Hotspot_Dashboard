"""Routes only — polling lives in monitor.py, persistence in storage.py."""
import threading
import hashlib
import inspect
import datetime
import os
import re
import subprocess
import time
import uuid

import paramiko
import waitress
from flask import Flask, jsonify, render_template, request, redirect, Response

import config
import models
import storage as storage_mod
import monitor as monitor_mod
import qrz as qrz_mod
from monitor import FleetMonitor
from storage import load_hotspots, save_hotspots, load_settings, save_settings, \
                   load_favorites, save_favorites, load_asl_favorites, save_asl_favorites, \
                   load_bm_tg_favorites, save_bm_tg_favorites, \
                   load_cameras, save_cameras, load_qsos, save_qsos, settings_transaction
from weather import WeatherClient
from qrz import QrzClient
from aprs import AprsClient
from brandmeister import BrandmeisterClient
from mqtt_publisher import MqttPublisher
from aprs_messaging import AprsMessenger
from aprs_inbox import AprsInbox
from host_stats import HostStats
import storage_activity
from update_check import UpdateChecker
from camera_stream import CameraStreamManager
from hf_conditions import HfConditionsClient
from license_quiz import LicenseQuizPool, DEFAULT_CLASS as LICENSE_QUIZ_DEFAULT_CLASS
from wspr_activity import WsprActivityClient, grid_to_latlon
from aurora import AuroraClient
from pota import PotaClient
from sota import SotaClient
from psk_reporter import PskReporterClient
from adif import parse_adif
from digipi import DigipiMonitor
from openspot import OpenSpot4Manager
from asl_audio import AslAudioManager
from wsjtx import WsjtxListener
from qrz_logbook import QrzLogbookClient
from hamalert import HamAlertListener
from brandmeister_lastheard import BrandmeisterLastHeardListener
from satellites import SatelliteTracker
from starlink_trains import StarlinkTrainClient
from flights import FlightsClient
from adsbdb import AdsbdbClient
from aslstats import AslStatsClient
from rockstar_bios import get_bio as get_codename_bio

import host_stats as host_stats_mod

app        = Flask(__name__)
monitor    = FleetMonitor()
wx         = WeatherClient()
# Separate from monitor.py's own private BrandmeisterClient (read-only,
# used by the slow-check loop) -- this one handles the hotspot card
# drawer's link/unlink talkgroup actions, which need the write endpoints
# and a fresh post-write lookup() to hand back to monitor.apply_bm_static_tgs().
bm_write_client = BrandmeisterClient()
host_stats = HostStats()
host_stats.start()
update_checker = UpdateChecker()
hf_conditions   = HfConditionsClient()
camera_manager  = CameraStreamManager()
license_quiz    = LicenseQuizPool()
satellite_tracker = SatelliteTracker()
starlink_train_client = StarlinkTrainClient()
wspr_activity   = WsprActivityClient()
flights_client  = FlightsClient()
adsbdb_client   = AdsbdbClient()
aurora_client   = AuroraClient()
# Separate from monitor.py's own private AslStatsClient (used internally
# for linked-node callsign resolution) -- this one backs the ASL Control
# sidebar's per-favorite Rx%/LCnt/status lookups, a different query shape
# (favorite_stats(), not linked_node_info()) with its own cache keys.
asl_stats_client = AslStatsClient()
pota_client     = PotaClient()
sota_client     = SotaClient()
psk_reporter    = PskReporterClient()
digipi_monitor  = DigipiMonitor()
openspot_manager = OpenSpot4Manager(monitor)
openspot_manager.reconcile(load_hotspots())  # eager start at boot, mirrors mqtt_pub's startup rebuild
# Separate persistent-connection manager, same reconcile()/remove() shape
# as openspot_manager -- see asl_audio.py's module docstring for why this
# is its own subsystem rather than folded into monitor.py's poll loop.
audio_manager = AslAudioManager()
audio_manager.reconcile(load_hotspots())
wsjtx_listener  = WsjtxListener(monitor)
qrz_logbook_client = QrzLogbookClient()
hamalert_listener = HamAlertListener()
brandmeister_lh = BrandmeisterLastHeardListener()

# Gates the Settings "Host power control" buttons -- only true on a
# standalone install running directly on real Raspberry Pi hardware (see
# host_stats.is_pi_standalone). Computed once at startup, not per-request.
HOST_CAN_POWER_CONTROL = host_stats_mod.is_pi_standalone()

# Gates the Version tab's "Install update" button -- true for any
# standalone (non-Docker) install, Pi or otherwise (install.sh also
# supports plain Debian/Ubuntu, not just Pi hardware -- narrower than
# HOST_CAN_POWER_CONTROL on purpose). A container can't safely rebuild
# and replace itself from inside without Docker-socket access, so Docker/
# Unraid always falls back to showing the manual update command instead.
HOST_IS_STANDALONE = not host_stats_mod.is_docker()

START_TIME = time.time()  # for /api/activity's dashboard_uptime_seconds


def _qrz_credentials() -> tuple[str, str]:
    """Return (username, password) — settings.json takes precedence over env vars."""
    settings = load_settings()
    username = settings.get("qrz_username", "").strip() or config.QRZ_USERNAME
    password = settings.get("qrz_password", "").strip() or config.QRZ_PASSWORD
    return username, password


def _rebuild_qrz_client() -> None:
    """Rebuild the QRZ client from the current credentials and hot-swap it in."""
    username, password = _qrz_credentials()
    monitor.set_qrz_client(QrzClient(username, password, config.QRZ_AGENT))


def _rebuild_aprs_client() -> None:
    settings = load_settings()
    monitor.set_aprs_client(AprsClient(settings.get("aprs_api_key", "")))


mqtt_pub = MqttPublisher()

def _rebuild_mqtt_client() -> None:
    """Rebuild the MQTT publisher from current settings and re-announce
    every hotspot. Closes the old client cleanly first (if any) so we
    don't leak connections when the broker/credentials are changed."""
    global mqtt_pub
    settings = load_settings()
    mqtt_pub.close()
    mqtt_pub = MqttPublisher(
        settings.get("mqtt_host", ""),
        settings.get("mqtt_port", 1883),
        settings.get("mqtt_username", ""),
        settings.get("mqtt_password", ""),
    )
    if mqtt_pub.enabled:
        mqtt_pub.set_hotspots(load_hotspots())


# Apply saved credentials immediately at startup
_rebuild_qrz_client()
_rebuild_aprs_client()
monitor.set_radioid_enabled(load_settings().get("radioid_enabled", True))
_rebuild_mqtt_client()

aprs_msg = AprsMessenger()

def _rebuild_aprs_messenger() -> None:
    """Rebuild the APRS messenger from current settings. Cooldown state
    (last-sent times, currently-alerted keys) is intentionally reset on
    rebuild -- a settings change is a reasonable moment to allow an
    immediate re-alert rather than carrying old cooldown timers forward."""
    global aprs_msg
    settings = load_settings()
    aprs_msg = AprsMessenger(
        settings.get("aprs_msg_callsign", ""),
        settings.get("aprs_msg_to_callsign", ""),
        settings.get("aprs_msg_cooldown_min", 10),
    )

_rebuild_aprs_messenger()

aprs_inbox = AprsInbox()

def _rebuild_aprs_inbox() -> None:
    """configure() itself is a no-op unless the callsign or enabled state
    actually changed, so this is cheap to call on every settings save
    regardless of which fields changed."""
    settings = load_settings()
    aprs_inbox.configure(
        settings.get("aprs_msg_callsign", ""),
        settings.get("aprs_inbox_enabled", False),
    )

_rebuild_aprs_inbox()

def _rebuild_wsjtx() -> None:
    """configure() itself is a no-op unless enabled state or port actually
    changed, so this is cheap to call on every settings save regardless of
    which fields changed -- same pattern as _rebuild_aprs_inbox above."""
    settings = load_settings()
    wsjtx_listener.configure(
        settings.get("wsjtx_enabled", False),
        settings.get("wsjtx_port", 2237),
    )

_rebuild_wsjtx()

def _rebuild_qrz_logbook() -> None:
    """configure() just stores enabled/key (no thread of its own -- the
    background _qrz_logbook_loop drives it), so this is cheap to call on
    every settings save regardless of which fields changed."""
    settings = load_settings()
    qrz_logbook_client.configure(
        settings.get("qrz_logbook_enabled", False),
        settings.get("qrz_logbook_api_key", ""),
    )

_rebuild_qrz_logbook()

def _rebuild_hamalert() -> None:
    """configure() itself is a no-op unless enabled/username/password
    actually changed, so this is cheap to call on every settings save
    regardless of which fields changed -- same pattern as
    _rebuild_aprs_inbox/_rebuild_wsjtx above."""
    settings = load_settings()
    hamalert_listener.configure(
        settings.get("hamalert_enabled", False),
        settings.get("hamalert_username", ""),
        settings.get("hamalert_password", ""),
    )

_rebuild_hamalert()

def _rebuild_brandmeister_lh() -> None:
    """configure() itself is a no-op unless enabled state actually
    changed -- same pattern as _rebuild_aprs_inbox/_rebuild_hamalert
    above. No credentials involved (the feed is public), just on/off."""
    settings = load_settings()
    brandmeister_lh.configure(settings.get("brandmeister_alerts_enabled", False))

_rebuild_brandmeister_lh()

import radioid as radioid_mod
import aprs as aprs_mod
import brandmeister as brandmeister_mod
import mqtt_publisher as mqtt_publisher_mod
import aprs_messaging as aprs_messaging_mod

# Computed once at startup so the endpoint is always fast
_BUILD_MODULES = [config, models, storage_mod, monitor_mod, qrz_mod, host_stats_mod,
                   radioid_mod, aprs_mod, brandmeister_mod, mqtt_publisher_mod, aprs_messaging_mod]
_BUILD_TIME    = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _module_info(mod) -> dict:
    src  = inspect.getsourcefile(mod)
    md5  = hashlib.md5(open(src, "rb").read()).hexdigest()
    mtime = datetime.datetime.fromtimestamp(
        os.path.getmtime(src), datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"file": os.path.basename(src), "md5": md5, "modified": mtime}

_MODULE_HASHES = [_module_info(m) for m in _BUILD_MODULES]


# --- data API ---

@app.route("/api/version")
def api_version():
    return jsonify({
        "container_start": _BUILD_TIME,
        "modules": _MODULE_HASHES,
        "app_version": config.APP_VERSION,
        "app_codename": config.APP_CODENAME,
    })

@app.route("/api/data")
def api_data():
    snap      = monitor.snapshot()
    hotspots  = load_hotspots()
    by_id     = {h["id"]: h for h in hotspots}
    for hotspot_id, entry in snap.items():
        hs = by_id.get(hotspot_id, {})
        entry["lat"]      = hs.get("lat")
        entry["lon"]      = hs.get("lon")
        entry["type"]     = hs.get("type", "wpsd")
        # Static config passthrough, same reason lat/lon/type are here --
        # renderCards() uses this to show the neutral "away" card instead
        # of the red offline one when a portable hotspot can't be reached.
        entry["portable"] = bool(hs.get("portable", False))
        # Card title's link override -- static config, same reason type/
        # lat/lon are passed through here rather than being part of the
        # live-polled HotspotStatus. None when unset -- renderCards()'s
        # cardNameHref falls back to the bare IP in that case.
        entry["card_url"] = hs.get("card_url")
        # DVSwitch card -- dvswitch_enabled/dvswitch_ports come straight from
        # hotspots.json (static config, same reason type/lat/lon do); the
        # sparkline is the one piece not already on the live snapshot, since
        # it's a activity_log query rather than in-memory poll state.
        # Keyed by hotspot_id (not ip) -- see storage_activity.py.
        if hs.get("dvswitch_enabled"):
            entry["dvswitch_enabled"] = True
            entry["dvswitch_sparkline"] = storage_activity.dvswitch_sparkline(hotspot_id)
        # Live audio-level VU meter (see asl_audio.py) -- static config
        # passthrough, same reason type/lat/lon/card_url are here rather
        # than part of the live-polled HotspotStatus. The level itself is
        # NOT included here -- it changes far faster than this 3s poll,
        # so the frontend fetches it separately via /api/audio_level on
        # its own faster interval, only once it sees this flag.
        if hs.get("audio_meter_enabled"):
            entry["audio_meter_enabled"] = True
    ordered_ids = [h["id"] for h in hotspots]
    # Return as an ARRAY so the browser preserves order — JS objects keyed by
    # id strings get silently re-sorted by some engines (especially for
    # keys that look numeric), so a dict is not reliable here.
    # Only ever return entries for ids currently in load_hotspots() -- never
    # fall back to "whatever is in the snapshot," since an in-flight poll
    # thread can resurrect a just-deleted hotspot's entry in monitor._data
    # (check_one -> _ensure_entry recreates it before the SSH call
    # finishes), which would otherwise make a deleted card reappear.
    ordered = [snap[key] for key in ordered_ids if key in snap]
    return jsonify(ordered)

@app.route("/api/audio_level")
def api_audio_level():
    """Cheap in-memory read of every audio_meter_enabled ASL3 node's
    current level (see asl_audio.py) -- no SSH, no network call per
    request, just a lock-guarded dict read. Polled by the browser on its
    own fast (~250ms) interval, decoupled from /api/data's 3s cycle, so
    the VU meter actually tracks live audio instead of stepping between
    infrequent samples the way a WPSD card's RSSI-driven meter does."""
    return jsonify(audio_manager.snapshot())

@app.route("/api/map_data")
def api_map_data():
    return jsonify(monitor.map_data())

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.json or {}
    # Held across the ENTIRE load-modify-save sequence below (through the
    # save_settings(settings) call further down), not just the individual
    # file read/write -- see storage.settings_transaction()'s docstring
    # for the real, reproduced race this prevents: setup.html's
    # saveCardOrder() fires several /api/settings POSTs in parallel (one
    # per changed field), and without a lock spanning the whole sequence,
    # two concurrent requests could each read the same stale snapshot and
    # then each write back their own full dict, silently discarding
    # whichever one wrote first. Manual __enter__/exit (via _settings_txn
    # below) rather than wrapping this ~150-line handler in a `with`
    # block, purely to avoid re-indenting all of it.
    _settings_txn = settings_transaction()
    _settings_txn.__enter__()
    settings = load_settings()
    if "dashboard_name" in data:
        settings["dashboard_name"] = data["dashboard_name"]
    if "dark_mode" in data:
        settings["dark_mode"] = bool(data["dark_mode"])
    if "links" in data:
        settings["links"] = data["links"]
    if "weather_location" in data:
        settings["weather_location"] = data["weather_location"]
    if "weather_unit" in data:
        settings["weather_unit"] = data["weather_unit"] if data["weather_unit"] in ("F", "C") else "F"
    if "show_host_stats" in data:
        settings["show_host_stats"] = bool(data["show_host_stats"])
    if "show_toolbar" in data:
        settings["show_toolbar"] = bool(data["show_toolbar"])
    if "beta_courtesy_tone" in data:
        settings["beta_courtesy_tone"] = bool(data["beta_courtesy_tone"])
    if "beta_spotlight_dimming" in data:
        settings["beta_spotlight_dimming"] = bool(data["beta_spotlight_dimming"])
    if "show_fleet_activity" in data:
        settings["show_fleet_activity"] = bool(data["show_fleet_activity"])
    if "fleet_activity_position" in data:
        try:
            settings["fleet_activity_position"] = max(0, int(data["fleet_activity_position"]))
        except (TypeError, ValueError):
            pass
    if "fleet_activity_hours" in data:
        try:
            hours = int(data["fleet_activity_hours"])
            if hours in config.FLEET_ACTIVITY_HOUR_OPTIONS:
                settings["fleet_activity_hours"] = hours
        except (TypeError, ValueError):
            pass
    if "show_asl_favorites" in data:
        settings["show_asl_favorites"] = bool(data["show_asl_favorites"])
    if "asl_favorites_position" in data:
        try:
            settings["asl_favorites_position"] = max(0, int(data["asl_favorites_position"]))
        except (TypeError, ValueError):
            pass
    if "qrz_username" in data:
        settings["qrz_username"] = data["qrz_username"].strip().upper()
    if "qrz_password" in data:
        settings["qrz_password"] = data["qrz_password"].strip()
    if "radioid_enabled" in data:
        settings["radioid_enabled"] = bool(data["radioid_enabled"])
    if "aprs_api_key" in data:
        settings["aprs_api_key"] = data["aprs_api_key"].strip()
    if "mqtt_host" in data:
        settings["mqtt_host"] = data["mqtt_host"].strip()
    if "mqtt_port" in data:
        try:
            settings["mqtt_port"] = int(data["mqtt_port"])
        except (TypeError, ValueError):
            pass
    if "mqtt_username" in data:
        settings["mqtt_username"] = data["mqtt_username"].strip()
    if "mqtt_password" in data:
        settings["mqtt_password"] = data["mqtt_password"]
    if "aprs_msg_callsign" in data:
        settings["aprs_msg_callsign"] = data["aprs_msg_callsign"].strip().upper()
    if "aprs_msg_to_callsign" in data:
        settings["aprs_msg_to_callsign"] = data["aprs_msg_to_callsign"].strip().upper()
    if "aprs_msg_cooldown_min" in data:
        try:
            settings["aprs_msg_cooldown_min"] = float(data["aprs_msg_cooldown_min"])
        except (TypeError, ValueError):
            pass
    if "update_check_enabled" in data:
        settings["update_check_enabled"] = bool(data["update_check_enabled"])
    if "update_check_repo" in data:
        settings["update_check_repo"] = data["update_check_repo"].strip()
    if "update_check_branch" in data:
        settings["update_check_branch"] = data["update_check_branch"].strip()
    if "show_cameras" in data:
        settings["show_cameras"] = bool(data["show_cameras"])
    if "show_dvswitch" in data:
        settings["show_dvswitch"] = bool(data["show_dvswitch"])
    if "aprs_inbox_enabled" in data:
        settings["aprs_inbox_enabled"] = bool(data["aprs_inbox_enabled"])
    if "aprs_inbox_position" in data:
        try:
            settings["aprs_inbox_position"] = max(0, int(data["aprs_inbox_position"]))
        except (TypeError, ValueError):
            pass
    if "digipi_enabled" in data:
        settings["digipi_enabled"] = bool(data["digipi_enabled"])
    if "digipi_ip" in data:
        settings["digipi_ip"] = data["digipi_ip"].strip()
    if "digipi_user" in data:
        settings["digipi_user"] = data["digipi_user"].strip()
    if "digipi_pass" in data:
        settings["digipi_pass"] = data["digipi_pass"]
    if "digipi_position" in data:
        try:
            settings["digipi_position"] = max(0, int(data["digipi_position"]))
        except (TypeError, ValueError):
            pass
    if "show_hf_conditions" in data:
        settings["show_hf_conditions"] = bool(data["show_hf_conditions"])
    if "hf_conditions_position" in data:
        try:
            settings["hf_conditions_position"] = max(0, int(data["hf_conditions_position"]))
        except (TypeError, ValueError):
            pass
    if "show_band_plan" in data:
        settings["show_band_plan"] = bool(data["show_band_plan"])
    if "band_plan_position" in data:
        try:
            settings["band_plan_position"] = max(0, int(data["band_plan_position"]))
        except (TypeError, ValueError):
            pass
    if "show_license_quiz" in data:
        settings["show_license_quiz"] = bool(data["show_license_quiz"])
    if "license_quiz_position" in data:
        try:
            settings["license_quiz_position"] = max(0, int(data["license_quiz_position"]))
        except (TypeError, ValueError):
            pass
    if "show_wspr_activity" in data:
        settings["show_wspr_activity"] = bool(data["show_wspr_activity"])
    if "wspr_activity_position" in data:
        try:
            settings["wspr_activity_position"] = max(0, int(data["wspr_activity_position"]))
        except (TypeError, ValueError):
            pass
    if "station_grid" in data:
        settings["station_grid"] = data["station_grid"].strip().upper()
    if "show_big_clock" in data:
        settings["show_big_clock"] = bool(data["show_big_clock"])
    if "big_clock_position" in data:
        try:
            settings["big_clock_position"] = max(0, int(data["big_clock_position"]))
        except (TypeError, ValueError):
            pass
    if "show_satellites" in data:
        settings["show_satellites"] = bool(data["show_satellites"])
    if "satellites_position" in data:
        try:
            settings["satellites_position"] = max(0, int(data["satellites_position"]))
        except (TypeError, ValueError):
            pass
    if "show_starlink_trains" in data:
        settings["show_starlink_trains"] = bool(data["show_starlink_trains"])
    if "show_flights_overhead" in data:
        settings["show_flights_overhead"] = bool(data["show_flights_overhead"])
    if "flights_overhead_position" in data:
        try:
            settings["flights_overhead_position"] = max(0, int(data["flights_overhead_position"]))
        except (TypeError, ValueError):
            pass
    if "tracked_satellites" in data:
        # norad_id gets interpolated into a CelesTrak URL (satellites.py) --
        # validated as an int here for the same reason asl_node is checked
        # with .isdigit() elsewhere, even though this is a read-only GET to
        # a public API. Malformed entries are dropped individually rather
        # than rejecting the whole list.
        cleaned = []
        for sat in data["tracked_satellites"] if isinstance(data["tracked_satellites"], list) else []:
            if not isinstance(sat, dict):
                continue
            try:
                norad_id = int(sat["norad_id"])
            except (KeyError, TypeError, ValueError):
                continue
            def _optional_float(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            cleaned.append({
                "norad_id": norad_id,
                "name": str(sat.get("name") or "").strip()[:40],
                "mode": str(sat.get("mode") or "").strip()[:20],
                "downlink_mhz": _optional_float(sat.get("downlink_mhz")),
                "uplink_mhz": _optional_float(sat.get("uplink_mhz")),
            })
        settings["tracked_satellites"] = cleaned
    if "show_recent_contacts" in data:
        settings["show_recent_contacts"] = bool(data["show_recent_contacts"])
    if "recent_contacts_position" in data:
        try:
            settings["recent_contacts_position"] = max(0, int(data["recent_contacts_position"]))
        except (TypeError, ValueError):
            pass
    if "show_qso_stats" in data:
        settings["show_qso_stats"] = bool(data["show_qso_stats"])
    if "qso_stats_position" in data:
        try:
            settings["qso_stats_position"] = max(0, int(data["qso_stats_position"]))
        except (TypeError, ValueError):
            pass
    if "show_top_activity" in data:
        settings["show_top_activity"] = bool(data["show_top_activity"])
    if "top_activity_position" in data:
        try:
            settings["top_activity_position"] = max(0, int(data["top_activity_position"]))
        except (TypeError, ValueError):
            pass
    if "dvswitch_position" in data:
        try:
            settings["dvswitch_position"] = max(0, int(data["dvswitch_position"]))
        except (TypeError, ValueError):
            pass
    if "wsjtx_enabled" in data:
        settings["wsjtx_enabled"] = bool(data["wsjtx_enabled"])
    if "wsjtx_port" in data:
        try:
            settings["wsjtx_port"] = max(1, min(65535, int(data["wsjtx_port"])))
        except (TypeError, ValueError):
            pass
    if "psk_reporter_callsign" in data:
        settings["psk_reporter_callsign"] = data["psk_reporter_callsign"].strip().upper()
    if "onboarding_tour_seen" in data:
        settings["onboarding_tour_seen"] = bool(data["onboarding_tour_seen"])
    if "card_order_tiebreak" in data:
        raw = data["card_order_tiebreak"]
        if isinstance(raw, list):
            settings["card_order_tiebreak"] = [str(x) for x in raw]
    if "hamalert_enabled" in data:
        settings["hamalert_enabled"] = bool(data["hamalert_enabled"])
    if "hamalert_username" in data:
        settings["hamalert_username"] = data["hamalert_username"].strip()
    if "hamalert_password" in data:
        settings["hamalert_password"] = data["hamalert_password"]
    if "hamalert_position" in data:
        try:
            settings["hamalert_position"] = max(0, int(data["hamalert_position"]))
        except (TypeError, ValueError):
            pass
    if "notifications_position" in data:
        try:
            settings["notifications_position"] = max(0, int(data["notifications_position"]))
        except (TypeError, ValueError):
            pass
    if "fleet_alerts_enabled" in data:
        settings["fleet_alerts_enabled"] = bool(data["fleet_alerts_enabled"])
    if "solar_alerts_enabled" in data:
        settings["solar_alerts_enabled"] = bool(data["solar_alerts_enabled"])
    if "brandmeister_alerts_enabled" in data:
        settings["brandmeister_alerts_enabled"] = bool(data["brandmeister_alerts_enabled"])
    if "brandmeister_api_key" in data:
        settings["brandmeister_api_key"] = data["brandmeister_api_key"]
    if "qrz_logbook_enabled" in data:
        settings["qrz_logbook_enabled"] = bool(data["qrz_logbook_enabled"])
    if "qrz_logbook_api_key" in data:
        settings["qrz_logbook_api_key"] = data["qrz_logbook_api_key"].strip()
    save_settings(settings)
    _settings_txn.__exit__(None, None, None)
    # Rebuilds below intentionally happen AFTER releasing the lock -- they
    # don't touch settings.json themselves, and some (MQTT reconnect, etc.)
    # can take a moment, which would otherwise hold up every OTHER
    # concurrent /api/settings request for no reason.
    # Rebuild QRZ client if credentials changed
    if "qrz_username" in data or "qrz_password" in data:
        _rebuild_qrz_client()
    if "radioid_enabled" in data:
        monitor.set_radioid_enabled(settings["radioid_enabled"])
    if "aprs_api_key" in data:
        _rebuild_aprs_client()
    if any(k in data for k in ("mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password")):
        _rebuild_mqtt_client()
    if any(k in data for k in ("aprs_msg_callsign", "aprs_msg_to_callsign", "aprs_msg_cooldown_min")):
        _rebuild_aprs_messenger()
    if any(k in data for k in ("aprs_msg_callsign", "aprs_inbox_enabled")):
        _rebuild_aprs_inbox()
    if any(k in data for k in ("wsjtx_enabled", "wsjtx_port")):
        _rebuild_wsjtx()
    if any(k in data for k in ("hamalert_enabled", "hamalert_username", "hamalert_password")):
        _rebuild_hamalert()
    if "brandmeister_alerts_enabled" in data:
        _rebuild_brandmeister_lh()
    if any(k in data for k in ("qrz_logbook_enabled", "qrz_logbook_api_key")):
        _rebuild_qrz_logbook()
    return jsonify({"ok": True})


# --- pages ---

@app.route("/")
def dashboard():
    return render_template("dashboard.html", settings=load_settings())

@app.route("/mobile")
def dashboard_mobile():
    """Lightweight single-column companion view for phones (see
    mobile-dashboard-handoff.md, PR 1). Purely a frontend template --
    it polls the existing /api/data and /api/qsos and owns no backend
    state of its own. The desktop dashboard at "/" is untouched."""
    return render_template("dashboard-mobile.html", settings=load_settings())

@app.route("/beta")
def dashboard_beta_redirect():
    """The "instrument panel" reskin (formerly a side-by-side /beta
    alternate, evaluated long enough to be promoted to be THE dashboard
    -- see CLAUDE.md) is just dashboard.html now. Kept as a redirect
    rather than removed outright so any old bookmark/link to /beta still
    lands somewhere real instead of 404ing."""
    return redirect("/")

@app.route("/api/favorites", methods=["GET"])
def api_favorites_get():
    return jsonify(load_favorites())

@app.route("/api/favorites", methods=["POST"])
def api_favorites_post():
    """Accept full list of favorites and overwrite."""
    data = request.json or []
    # Normalise: uppercase callsigns, strip whitespace
    cleaned = [
        {"call": f["call"].upper().strip(), "label": f.get("label", "").strip()}
        for f in data if f.get("call", "").strip()
    ]
    save_favorites(cleaned)
    return jsonify({"ok": True})

@app.route("/api/asl_favorites", methods=["GET"])
def api_asl_favorites_get():
    return jsonify(load_asl_favorites())

@app.route("/api/asl_favorites", methods=["POST"])
def api_asl_favorites_post():
    """Accept full list of ASL favorite node numbers and overwrite --
    distinct from the callsign favorites above. Preserves pinned/pinned_at
    (see storage.load_asl_favorites()'s docstring) -- this route is the
    active, frequent write path (every pin toggle/add/remove from the
    redesigned card or drawer goes through it), so dropping those fields
    here would wipe a just-set pin the moment it's saved, not just leave
    it for the read-side migration to backfill later."""
    data = request.json or []
    cleaned = []
    for f in data:
        node = f.get("node", "").strip()
        if not node.isdigit():
            continue
        entry = {"node": node, "label": f.get("label", "").strip()}
        if "pinned" in f:
            entry["pinned"] = bool(f["pinned"])
        try:
            entry["pinned_at"] = float(f.get("pinned_at", 0))
        except (TypeError, ValueError):
            entry["pinned_at"] = 0
        cleaned.append(entry)
    save_asl_favorites(cleaned)
    return jsonify({"ok": True})

@app.route("/api/asl_favorite_stats")
def api_asl_favorite_stats():
    """Per-favorite live stats (Active status, Web-Transceiver flag, Rx%,
    LCnt, recently_active) from stats.allstarlink.org for the ASL Control
    sidebar -- accepts ?nodes=27339,29332,... and returns one
    AslStatsClient.favorite_stats() result per node, cached server-side
    (see that method's docstring for why it prefers this app's own
    SSH-sourced keyed status over the stats API's own unreliable one)."""
    nodes = [n.strip() for n in request.args.get("nodes", "").split(",") if n.strip().isdigit()]
    return jsonify({n: asl_stats_client.favorite_stats(n) for n in nodes})

@app.route("/api/bm_tg_favorites", methods=["GET"])
def api_bm_tg_favorites_get():
    return jsonify(load_bm_tg_favorites())

@app.route("/api/bm_tg_favorites", methods=["POST"])
def api_bm_tg_favorites_post():
    """Accept full list of Brandmeister talkgroup quick-link favorites and
    overwrite -- same shape/overwrite convention as ASL favorites above,
    distinct list entirely (tg+slot, not a node number)."""
    data = request.json or []
    cleaned = []
    for f in data:
        tg = str(f.get("tg", "")).strip()
        slot = str(f.get("slot", "")).strip()
        if tg.isdigit() and slot in ("1", "2"):
            cleaned.append({"tg": tg, "slot": slot, "label": f.get("label", "").strip()})
    save_bm_tg_favorites(cleaned)
    return jsonify({"ok": True})

# Canonical declaration order for every "extra card" sentinel -- must
# match dashboard.html's renderCards() sentinels.push() order exactly
# (cameras last in both places). Used only to compute overflow_sentinels
# below; the inline (interleaved-with-hotspots) rendering in setup.html
# still reads settings directly and isn't affected by this list.
_SENTINEL_DEFS = [
    ("__fleet_activity__", "show_fleet_activity", "fleet_activity_position", "📊", "Fleet activity", "metrics card"),
    ("__asl_favorites__", "show_asl_favorites", "asl_favorites_position", "📻", "ASL Favorites", "control card"),
    ("__hf_conditions__", "show_hf_conditions", "hf_conditions_position", "☀️", "HF Conditions", "propagation card"),
    ("__band_plan__", "show_band_plan", "band_plan_position", "📻", "Band Plan", "reference card"),
    ("__license_quiz__", "show_license_quiz", "license_quiz_position", "🎓", "License Quiz", "practice card"),
    ("__wspr_activity__", "show_wspr_activity", "wspr_activity_position", "📶", "Band Activity", "WSPR activity card"),
    ("__digipi__", "digipi_enabled", "digipi_position", "📡", "DigiPi", "APRS/Direwolf card"),
    ("__big_clock__", "show_big_clock", "big_clock_position", "🕐", "Big Ass Clock", "clock card"),
    ("__notifications__", ("aprs_inbox_enabled", "hamalert_enabled", "fleet_alerts_enabled", "solar_alerts_enabled", "brandmeister_alerts_enabled", "qrz_logbook_enabled"), "notifications_position", "🔔", "Notifications", "APRS + HamAlert inbox card"),
    ("__satellites__", "show_satellites", "satellites_position", "🛰️", "Satellites", "pass prediction card"),
    ("__flights_overhead__", "show_flights_overhead", "flights_overhead_position", "✈️", "Flights Overhead", "nearby aircraft card"),
    ("__recent_contacts__", "show_recent_contacts", "recent_contacts_position", "📻", "Recent Contacts", "logged QSO card"),
    ("__qso_stats__", "show_qso_stats", "qso_stats_position", "📈", "QSO Stats", "logbook summary card"),
    ("__top_activity__", "show_top_activity", "top_activity_position", "🏆", "Top 5 Activity", "fleet callsign activity ranking card"),
]

_CAMERA_TYPE_LABELS = {"rtsp": "RTSP", "wyze": "Wyze", "bambu_a1": "Bambu A1"}


def _overflow_sentinels(settings: dict, hotspots: list, cameras: list) -> list:
    """Enabled cards/cameras whose saved position is at or past the end
    of the hotspot list, sorted by that position value with an explicit
    tiebreak (settings.card_order_tiebreak, falling back to _SENTINEL_DEFS'
    own declared order for anything not in that list).

    A card's *_position field only ever records "how many hotspot rows
    precede this card" -- confirmed live that this is a genuine, deeper
    bug than it first looked: two or more cards dragged to sit on the
    SAME side of every hotspot (extremely common -- e.g. "all after the
    last hotspot" with only 1-2 real hotspots configured) compute the
    IDENTICAL saved position no matter what relative order they were
    actually dragged into, because "count of preceding hotspots" is
    genuinely all that number can represent. A first fix (sorting
    correctly by that position value instead of always using fixed
    template order) only helped when two cards' saved positions actually
    differed -- it did nothing for this same-boundary case, which is why
    "APRS Messages always renders first" persisted even after that fix:
    with only 1-2 hotspots, APRS Messages and several other enabled cards
    all save the exact same position, and the earliest-declared one
    (APRS Messages, per _SENTINEL_DEFS) always wins that tie regardless
    of how many times you drag-and-save. The real fix is
    card_order_tiebreak -- an explicit list of "__key__" ids in last-
    dragged order, populated by setup.html's saveCardOrder(), used here
    purely as a secondary sort key. Empty by default, which preserves
    today's exact _SENTINEL_DEFS-order fallback for anything the user
    hasn't explicitly reordered relative to a same-boundary sibling yet.
    """
    hotspot_count = len(hotspots)
    tiebreak = settings.get("card_order_tiebreak", []) or []
    items = []
    for data_ip, enabled_key, pos_key, icon, name, meta in _SENTINEL_DEFS:
        # enabled_key is a tuple for a card backed by more than one
        # independent integration (e.g. Notifications = APRS inbox OR
        # HamAlert) -- enabled if ANY of them is, not all.
        if isinstance(enabled_key, tuple):
            is_enabled = any(settings.get(k, False) for k in enabled_key)
        else:
            is_enabled = settings.get(enabled_key, False)
        if not is_enabled:
            continue
        pos = settings.get(pos_key, 0)
        if pos >= hotspot_count:
            items.append({"data_ip": data_ip, "icon": icon, "name": name, "meta": meta, "pos": pos})
    if settings.get("show_cameras", False):
        for cam in cameras:
            pos = cam.get("position", 0)
            if pos >= hotspot_count:
                type_label = _CAMERA_TYPE_LABELS.get(cam.get("type"), cam.get("type"))
                items.append({
                    "data_ip": f"__camera__{cam['id']}", "icon": "📷", "name": cam["name"],
                    "meta": f"camera · {type_label}", "pos": pos,
                })
    # DVSwitch card (v3.99) -- ONE consolidated card covering every
    # DVSwitch-enabled ASL3 hotspot via its own "Show:" node picker, not
    # one card per hotspot (that was the original v3.66 shape -- see
    # CLAUDE.md for why it was collapsed). Shown when ANY hotspot has
    # dvswitch_enabled AND the "Show DVSwitch card" toggle is on
    # (show_dvswitch, default True so existing installs with a DVSwitch
    # hotspot are unaffected). The single position lives in settings.json
    # like every other single-instance card above, not per-hotspot.
    if settings.get("show_dvswitch", True) and any(hs.get("dvswitch_enabled") for hs in hotspots):
        pos = settings.get("dvswitch_position", 0)
        if pos >= hotspot_count:
            items.append({
                "data_ip": "__dvswitch__", "icon": "📻",
                "name": "DVSwitch", "meta": "DVSwitch card", "pos": pos,
            })

    def sort_key(it):
        tb_rank = tiebreak.index(it["data_ip"]) if it["data_ip"] in tiebreak else len(tiebreak)
        return (it["pos"], tb_rank)

    items.sort(key=sort_key)
    return items


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ip   = request.form.get("ip", "").strip()
        # A blank name/ip previously saved a dead entry with no way to
        # delete it -- the delete button posts to /api/delete_hotspot/<ip>,
        # which 404s on an empty ip, permanently stranding the row.
        if not name or not ip:
            return redirect("/setup")
        hotspots    = load_hotspots()
        # Identity is the hotspot's own generated id, not ip (two hotspots
        # CAN share one ip -- two ASL3 radios/node numbers behind one SSH
        # login is a real, confirmed setup). Blank on add-new (no hidden
        # field value yet); the existing hotspot's id on edit. This is why
        # editing IP no longer needs the old orig_ip "match on the pre-edit
        # value" dance -- ip is just an ordinary field now.
        hotspot_id  = request.form.get("id", "").strip() or f"hs-{uuid.uuid4().hex[:10]}"
        new_hotspot = {
            "id":   hotspot_id,
            "name": name,
            "ip":   ip,
            "user": request.form.get("user"),
            "pass": request.form.get("pass"),
            # Checkboxes are only present in form data when checked -- absent
            # means unchecked, not "field not submitted", so this correctly
            # defaults to disabled if the checkbox was unticked.
            "enabled": "enabled" in request.form,
            # Generic across every hotspot type (not just openSPOT4) -- a
            # portable hotspot going offline gets the neutral "away" card
            # treatment instead of the red offline/error one, since not
            # being reachable is expected for something you take mobile,
            # not a problem. Same checkbox convention as "enabled" above.
            "portable": "portable" in request.form,
        }
        lat = request.form.get("lat", "").strip()
        lon = request.form.get("lon", "").strip()
        if lat and lon:
            try:
                new_hotspot["lat"] = float(lat)
                new_hotspot["lon"] = float(lon)
            except ValueError:
                pass
        bm_id = request.form.get("brandmeister_id", "").strip()
        if bm_id:
            new_hotspot["brandmeister_id"] = bm_id
        node_type = request.form.get("type", "wpsd").strip()
        if node_type == "asl3":
            new_hotspot["type"] = "asl3"
            asl_node = request.form.get("asl_node", "").strip()
            # Interpolated into a shell string over SSH (config.build_asl_status_cmd)
            # -- validate digits-only here too, since /setup has no auth.
            if asl_node.isdigit():
                new_hotspot["asl_node"] = asl_node
            # DVSwitch (Analog_Bridge) is an optional add-on many ASL3 nodes
            # don't run -- opt-in, same "absent means off" checkbox
            # convention as "enabled" above, not assumed from asl_node alone.
            new_hotspot["dvswitch_enabled"] = "dvswitch_enabled" in request.form
            # DVSwitch card only -- one or more Analog_Bridge instance ports
            # (multi-bridge is a real, confirmed setup, addressed by port via
            # /tmp/ABInfo_<port>.json -- see CLAUDE.md). Free-text, comma/
            # newline separated, same convention as openspot4_extra_pass
            # below; re-validated digits-only here since these are
            # interpolated into a shell string over SSH
            # (config.build_asl_status_cmd) and /setup has no auth.
            raw_ports = request.form.get("dvswitch_ports", "")
            ports = [p.strip() for p in re.split(r"[,\n]+", raw_ports) if p.strip().isdigit()]
            if ports:
                new_hotspot["dvswitch_ports"] = ",".join(ports)
            # Optional override for the card title's link -- defaults to the
            # bare IP (see renderCards()'s cardNameHref) when blank. Scheme
            # restricted to http(s) so a stray "javascript:" value can't
            # become a click-to-run XSS vector -- this app's own /setup has
            # no auth, so this is real defense-in-depth, not theoretical.
            card_url = request.form.get("card_url", "").strip()
            if card_url and re.match(r"^https?://", card_url, re.IGNORECASE):
                new_hotspot["card_url"] = card_url
            # Optional override for WHICH sa818-menu-saved config file this
            # hotspot's frequency/tone reading comes from -- see
            # config.build_sa818_conf_cmd()'s docstring for why (two
            # physical SA818 modules on one box, one SSH login, would
            # otherwise both read the same default /etc/sa818.conf and
            # report identical frequencies for two different radios).
            # Blank means the default path. Re-validated the same
            # shell-interpolated-over-SSH way as asl_node/dvswitch_ports
            # above; an invalid value is silently dropped rather than
            # saved, same posture as card_url's scheme check.
            sa818_conf_path = request.form.get("sa818_conf_path", "").strip()
            if sa818_conf_path and re.match(config.SA818_CONF_PATH_PATTERN, sa818_conf_path):
                new_hotspot["sa818_conf_path"] = sa818_conf_path
            # Live audio-level VU meter (see asl_audio.py) -- opt-in, same
            # "absent means off" checkbox convention as dvswitch_enabled
            # above. Needs the node to have already been provisioned via
            # provision-audio-meter.sh; the toggle itself is harmless to
            # flip on an unprovisioned node (the worker just retries with
            # backoff, same degrade-gracefully contract as every other
            # integration here).
            new_hotspot["audio_meter_enabled"] = "audio_meter_enabled" in request.form
        elif node_type == "openspot4":
            new_hotspot["type"] = "openspot4"
            # "pass" (already set unconditionally above) is the primary
            # password; "user" is stored but unused (no SSH/login-user
            # concept for openSPOT4). openspot4_extra_pass holds any other
            # config profiles' passwords (one per line) -- each openSPOT4
            # profile can have its own separate password, confirmed live,
            # so openspot.py tries all of these in order at login time.
            extra_pass = request.form.get("openspot4_extra_pass", "")
            if extra_pass.strip():
                new_hotspot["openspot4_extra_pass"] = extra_pass
        # Replace in place at its EXISTING index when editing -- a plain
        # filter-out-then-append (the previous approach) always moved the
        # edited hotspot to the end of the list, which /api/data's own
        # ordering reads straight from, so editing any hotspot's settings
        # silently reordered it to last on the dashboard every time. Only
        # a genuinely NEW hotspot (no existing index) goes at the end.
        existing_idx = next((i for i, h in enumerate(hotspots) if h.get("id") == hotspot_id), None)
        if existing_idx is not None:
            hotspots[existing_idx] = new_hotspot
        else:
            hotspots.append(new_hotspot)
        save_hotspots(hotspots)
        if mqtt_pub.enabled:
            mqtt_pub.set_hotspots(hotspots)
        openspot_manager.reconcile(hotspots)
        audio_manager.reconcile(hotspots)
        return redirect("/setup")
    setup_hotspots = load_hotspots()
    setup_settings = load_settings()
    setup_cameras  = load_cameras()
    return render_template("setup.html", hotspots=setup_hotspots,
                           settings=setup_settings, favorites=load_favorites(),
                           cameras=setup_cameras, asl_favorites=load_asl_favorites(),
                           bm_tg_favorites=load_bm_tg_favorites(),
                           can_power_control=HOST_CAN_POWER_CONTROL,
                           host_is_standalone=HOST_IS_STANDALONE,
                           overflow_sentinels=_overflow_sentinels(setup_settings, setup_hotspots, setup_cameras),
                           app_version=config.APP_VERSION, app_codename=config.APP_CODENAME)

@app.route("/api/hotspot_config/<hotspot_id>")
def api_hotspot_config(hotspot_id):
    """Raw stored hotspot config (including SSH/admin credentials) for the
    card drawer's Settings block -- /api/data deliberately excludes
    credentials from the live-polled snapshot (a 3s poll every open
    browser tab reads from is the wrong place for plaintext SSH
    passwords), so the drawer fetches this separately, once, right when
    it opens, the same way setup.html's edit form already gets these
    values server-rendered into the page."""
    hotspot = next((h for h in load_hotspots() if h.get("id") == hotspot_id), None)
    if hotspot is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(hotspot)

@app.route("/api/update_hotspot", methods=["POST"])
def api_update_hotspot():
    """JSON per-field upsert for the card drawer's instant-save Settings
    block -- same validation rules as /setup's form POST handler above
    (kept in sync manually -- if you touch one, check the other), but
    returns JSON instead of a redirect, and merges onto the EXISTING
    stored hotspot dict rather than rebuilding one from scratch. That
    merge matters: the drawer's per-field auto-save fires far more often
    than the full Settings form's one-shot Save, and rebuilding fresh each
    time would silently wipe any field the drawer doesn't expose every
    single time a user edits so much as their SSH password."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    ip   = (data.get("ip") or "").strip()
    if not name or not ip:
        return jsonify({"ok": False, "message": "Name and IP are required"}), 400

    hotspots   = load_hotspots()
    # Identity is the hotspot's id, not ip -- this route only ever edits an
    # EXISTING hotspot (the drawer's Settings block), so id always arrives
    # in the payload (currentHotspotConfig.id, client-side). A missing id
    # shouldn't happen in practice, but falls back to a fresh one (creates
    # a new entry) rather than a 400 -- same degrade-gracefully posture as
    # the rest of this route.
    hotspot_id = (data.get("id") or "").strip() or f"hs-{uuid.uuid4().hex[:10]}"
    existing   = next((h for h in hotspots if h.get("id") == hotspot_id), None)
    hotspot    = dict(existing) if existing else {}

    hotspot["id"]   = hotspot_id
    hotspot["name"] = name
    hotspot["ip"]   = ip
    hotspot["user"] = data.get("user", hotspot.get("user"))
    hotspot["pass"] = data.get("pass", hotspot.get("pass"))
    # Same "absent means unchecked" convention as /setup's form handler,
    # just expressed as an explicit JSON bool instead of form-field presence.
    hotspot["enabled"] = bool(data.get("enabled", hotspot.get("enabled", True)))
    hotspot["portable"] = bool(data.get("portable", hotspot.get("portable", False)))

    lat, lon = data.get("lat"), data.get("lon")
    if lat not in (None, "") and lon not in (None, ""):
        try:
            hotspot["lat"] = float(lat)
            hotspot["lon"] = float(lon)
        except (TypeError, ValueError):
            pass

    bm_id = (data.get("brandmeister_id") or "").strip()
    if bm_id:
        hotspot["brandmeister_id"] = bm_id
    else:
        hotspot.pop("brandmeister_id", None)

    # type isn't editable from this drawer (only full Settings can change
    # it) -- always forwarded from the already-loaded config, never reset.
    node_type = (data.get("type") or hotspot.get("type", "wpsd")).strip()
    hotspot["type"] = node_type
    if node_type == "asl3":
        asl_node = (data.get("asl_node") or "").strip()
        # Interpolated into a shell string over SSH (config.build_asl_status_cmd)
        # -- validate digits-only here too, same as /setup.
        if asl_node.isdigit():
            hotspot["asl_node"] = asl_node
        else:
            hotspot.pop("asl_node", None)
        hotspot["dvswitch_enabled"] = bool(data.get("dvswitch_enabled", hotspot.get("dvswitch_enabled", False)))
        raw_ports = data.get("dvswitch_ports", "") or ""
        ports = [p.strip() for p in re.split(r"[,\n]+", raw_ports) if p.strip().isdigit()]
        if ports:
            hotspot["dvswitch_ports"] = ",".join(ports)
        else:
            hotspot.pop("dvswitch_ports", None)
        card_url = (data.get("card_url") or "").strip()
        if card_url and re.match(r"^https?://", card_url, re.IGNORECASE):
            hotspot["card_url"] = card_url
        else:
            hotspot.pop("card_url", None)
        # Which sa818-menu-saved config file this hotspot's frequency
        # reading comes from -- see config.build_sa818_conf_cmd()'s
        # docstring. Same re-validation as /setup's form handler.
        sa818_conf_path = (data.get("sa818_conf_path") or "").strip()
        if sa818_conf_path and re.match(config.SA818_CONF_PATH_PATTERN, sa818_conf_path):
            hotspot["sa818_conf_path"] = sa818_conf_path
        else:
            hotspot.pop("sa818_conf_path", None)
        hotspot["audio_meter_enabled"] = bool(
            data.get("audio_meter_enabled", hotspot.get("audio_meter_enabled", False))
        )
    elif node_type == "openspot4":
        extra_pass = data.get("openspot4_extra_pass", "") or ""
        if extra_pass.strip():
            hotspot["openspot4_extra_pass"] = extra_pass
        else:
            hotspot.pop("openspot4_extra_pass", None)

    # Replace in place at its EXISTING index -- this route only ever edits
    # an existing hotspot (see the docstring above), and every field
    # auto-saves on its own blur/toggle, so a filter-out-then-append here
    # would move the card to the end of the dashboard on nearly every
    # single edit. Same fix as /setup's POST handler above.
    existing_idx = next((i for i, h in enumerate(hotspots) if h.get("id") == hotspot_id), None)
    if existing_idx is not None:
        hotspots[existing_idx] = hotspot
    else:
        hotspots.append(hotspot)
    save_hotspots(hotspots)
    if mqtt_pub.enabled:
        mqtt_pub.set_hotspots(hotspots)
    openspot_manager.reconcile(hotspots)
    audio_manager.reconcile(hotspots)
    return jsonify({"ok": True, "hotspot": hotspot})

@app.route("/api/host_stats")
def api_host_stats():
    # dashboard_uptime_seconds also lives on /api/activity's response (used
    # by the Fleet Activity card, which is only fetched when that card is
    # enabled) -- also included here since THIS endpoint is polled
    # unconditionally regardless of settings, which the Quick Settings
    # drawer's own uptime row relies on to always have a value.
    data = host_stats.snapshot()
    data["dashboard_uptime_seconds"] = time.time() - START_TIME
    return jsonify(data)

def _run_power_command(cmd: list[str]) -> tuple[bool, str]:
    """Run a reboot/poweroff command, waiting briefly to catch an immediate
    failure (e.g. polkit denial) without blocking on a real reboot -- which
    takes several seconds and kills this process anyway, so a successful
    call never actually finishes communicate() within the timeout."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _, stderr = proc.communicate(timeout=2)
            if proc.returncode not in (0, None):
                return False, (stderr or "").strip() or f"{cmd[0]} exited with code {proc.returncode}"
        except subprocess.TimeoutExpired:
            pass  # still running after 2s -- the system is very likely actually going down
        return True, "OK"
    except Exception as e:
        return False, str(e)

@app.route("/api/host_reboot", methods=["POST"])
def host_reboot():
    """Reboot the machine running the dashboard itself -- only enabled on a
    standalone install on real Raspberry Pi hardware (HOST_CAN_POWER_CONTROL).
    Uses `systemctl reboot` (talks to systemd over D-Bus) rather than `sudo`,
    since the service runs with NoNewPrivileges=yes, which blocks sudo/setuid
    entirely regardless of sudoers config. Needs a polkit rule granting the
    service user the org.freedesktop.login1.reboot action (see install.sh/
    update.sh and README.md's "Host power control" section) -- a headless
    systemd service has no active session, so default polkit policy would
    otherwise deny it."""
    if not HOST_CAN_POWER_CONTROL:
        return jsonify({"success": False, "message": "Not available on this deployment"}), 403
    ok, message = _run_power_command(["systemctl", "reboot"])
    return jsonify({"success": ok, "message": "Rebooting now" if ok else message})

@app.route("/api/host_poweroff", methods=["POST"])
def host_poweroff():
    """Power off the machine running the dashboard itself -- same mechanism
    and polkit requirement as host_reboot(). One-way: stays off until
    someone physically restores power."""
    if not HOST_CAN_POWER_CONTROL:
        return jsonify({"success": False, "message": "Not available on this deployment"}), 403
    ok, message = _run_power_command(["systemctl", "poweroff"])
    return jsonify({"success": ok, "message": "Powering off now" if ok else message})

@app.route("/api/activity")
def api_activity():
    default_hours    = load_settings().get("fleet_activity_hours", 12)
    default_interval = config.FLEET_ACTIVITY_HOUR_OPTIONS.get(default_hours, 15)
    hours            = request.args.get("hours", default=default_hours, type=int)
    interval_minutes = request.args.get("interval_minutes", default=default_interval, type=int)
    rank_by          = request.args.get("rank_by", default="count")
    result   = storage_activity.query_activity(hours=hours, interval_minutes=interval_minutes)
    hotspots = load_hotspots()
    snap     = monitor.snapshot()
    hotspots_online = sum(
        1 for h in hotspots if snap.get(h["id"], {}).get("status") != "Offline"
    )
    return jsonify({
        "buckets":                 result["buckets"],
        "mode_breakdown":          result["mode_breakdown"],
        "hotspots_online":         hotspots_online,
        "hotspots_total":          len(hotspots),
        "last_activity":           result["last_activity"],
        "dashboard_uptime_seconds": time.time() - START_TIME,
        "top_targets":             storage_activity.top_targets(hours=hours, limit=5, rank_by=rank_by),
    })

@app.route("/api/weather")
def api_weather():
    settings = load_settings()
    data = wx.get(settings.get("weather_location", ""),
                  settings.get("weather_unit", "F"))
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/hf_conditions")
def api_hf_conditions():
    data = hf_conditions.get()
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/wspr_activity")
def api_wspr_activity():
    """Live WSPR spot-count sparklines for the Band Activity card, within
    RADIUS_METERS of settings' station_grid -- see wspr_activity.py.
    503 covers both "no grid configured" and "fetch failed", same as
    every other integration's degrade-gracefully pattern."""
    station_grid = load_settings().get("station_grid", "")
    data = wspr_activity.get(station_grid)
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/flights")
def api_flights():
    """Live nearby-aircraft feed for the Flights Overhead card, within
    flights.py's own fixed RADIUS_KM of settings' station_grid -- see
    flights.py. 503 covers both "no grid configured" and "fetch failed",
    same degrade-gracefully pattern as every other integration."""
    station_grid = load_settings().get("station_grid", "")
    data = flights_client.get(station_grid)
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/flight_route")
def api_flight_route():
    """On-demand aircraft type/registration + flight route lookup for
    the Flights Overhead card's detail drawer -- see adsbdb.py.
    Deliberately fetched per-aircraft, on drawer open, not bundled into
    /api/flights' own list response -- most listed aircraft are never
    clicked into, so there's no reason to look up route data for all of
    them every poll. icao24 is required; callsign is optional but
    substantially improves route-match accuracy (adsbdb keys routes by
    callsign, not airframe)."""
    icao24 = request.args.get("icao24", "")
    callsign = request.args.get("callsign")
    data = adsbdb_client.lookup(icao24, callsign)
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/aurora")
def api_aurora():
    """Live NOAA OVATION aurora-oval overlay for the Live map's optional
    "Aurora oval" layer -- see aurora.py."""
    data = aurora_client.get()
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/pota_spots")
def api_pota_spots():
    """Live Parks on the Air activator spots for the Live map's optional
    "POTA spots" layer -- see pota.py. No per-user config needed, unlike
    most other overlays here."""
    data = pota_client.get()
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/sota_spots")
def api_sota_spots():
    """Live Summits on the Air activator spots for the Live map's optional
    "SOTA spots" layer -- see sota.py. No per-user config needed, same as
    POTA above."""
    data = sota_client.get()
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/api/psk_reporter")
def api_psk_reporter():
    """Live PSK Reporter reception reports for settings.psk_reporter_callsign
    -- see psk_reporter.py. 503 covers both "no callsign configured" and
    "fetch failed", same degrade-gracefully pattern as every other
    integration's route here. Also resolves settings.station_grid to
    qth_lat/qth_lon server-side (reusing grid_to_latlon, same as the ADIF
    importer/wsjtx.py) so the frontend can draw a "heard from here" line
    without needing its own grid-square math."""
    settings = load_settings()
    callsign = settings.get("psk_reporter_callsign", "")
    data = psk_reporter.get(callsign)
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    qth = grid_to_latlon(settings.get("station_grid", ""))
    data = dict(data)
    data["qth_lat"], data["qth_lon"] = qth if qth is not None else (None, None)
    return jsonify(data)

@app.route("/api/qsos")
def api_qsos():
    return jsonify(load_qsos())

def _adif_freq_to_hz(freq_mhz_str) -> "int | None":
    """ADIF's FREQ field is a plain string in MHz (e.g. "14.074000")."""
    if not freq_mhz_str:
        return None
    try:
        return round(float(freq_mhz_str) * 1_000_000)
    except (TypeError, ValueError):
        return None

def _adif_datetime_to_epoch(qso_date: str, time_on) -> "float | None":
    """QSO_DATE is YYYYMMDD, TIME_ON is HHMM or HHMMSS -- both UTC per the
    ADIF spec. Returns None (not a guess) if either is missing/malformed,
    so a QSO without a usable timestamp just sorts after every QSO that
    has one, rather than getting a fabricated time."""
    if not qso_date or len(qso_date) != 8:
        return None
    time_on = (time_on or "").strip()
    if len(time_on) == 4:
        time_on += "00"
    elif len(time_on) != 6:
        return None
    try:
        dt = datetime.datetime(
            int(qso_date[0:4]), int(qso_date[4:6]), int(qso_date[6:8]),
            int(time_on[0:2]), int(time_on[2:4]), int(time_on[4:6]),
            tzinfo=datetime.timezone.utc,
        )
        return dt.timestamp()
    except ValueError:
        return None

@app.route("/api/import_adif", methods=["POST"])
def api_import_adif():
    """Parses an uploaded ADIF log and replaces the stored QSO list
    wholesale (no merge/dedupe -- a station worked many times is
    expected). Position resolution: GRIDSQUARE first (direct, no lookup
    needed), falling back to the same QRZ/RadioID/APRS composition every
    other card uses (monitor.lookup_caller_info) when absent. A QSO with
    neither is skipped -- nothing to plot it with. Name/city/state/
    country enrichment always runs this lookup regardless of whether
    GRIDSQUARE gave us a position -- see the comment at the call site.

    Each QSO also gets a "worked from" QTH position, so the map can draw a
    line back to the home station: MY_GRIDSQUARE on that specific record
    takes priority (handles a portable/rover log where the operating
    location changes between QSOs), falling back to settings' station_grid
    otherwise. A QSO with neither gets no qth_lat/qth_lon -- plotted as a
    bare pin, no line, same as before this feature existed."""
    data = request.json or {}
    text = data.get("text", "")
    filename = data.get("filename", "")
    records = parse_adif(text)
    default_qth = grid_to_latlon(load_settings().get("station_grid", ""))

    qsos = []
    for r in records:
        call = (r.get("CALL") or "").strip().upper()
        if not call:
            continue
        grid = (r.get("GRIDSQUARE") or "").strip()
        latlon = grid_to_latlon(grid) if grid else None
        # Always resolve name/city/state/country via QRZ/RadioID, not just
        # when GRIDSQUARE is absent -- ADIF has no city/state fields at
        # all, and in practice most real logs carry GRIDSQUARE but NOT
        # COUNTRY (many logging programs never populate it), so the old
        # "only look up when grid is missing" rule left country/flag
        # blank for nearly every imported QSO, a real reported gap, not a
        # hypothetical. Matches wsjtx.py's live path, which already always
        # enriches regardless of grid. QrzClient.lookup() caches per
        # callsign and short-circuits near-instantly when QRZ isn't
        # configured (QrzClient.enabled), so this stays cheap for a large
        # import when QRZ is off; when QRZ IS configured, repeated
        # callsigns in a real log (common) hit the cache after the first.
        info = monitor.lookup_caller_info(call)
        if latlon is not None:
            lat, lon = latlon
        else:
            lat, lon = info["lat"], info["lon"]
        qrz_name, location = info["name"], info["location"]
        city, state, qrz_country = info["city"], info["state"], info["country"]
        if lat is None or lon is None:
            continue  # can't plot without a position

        my_grid = (r.get("MY_GRIDSQUARE") or "").strip()
        qth = grid_to_latlon(my_grid) if my_grid else None
        if qth is None:
            qth = default_qth
        qth_lat, qth_lon = qth if qth is not None else (None, None)

        qso_date = (r.get("QSO_DATE") or "").strip()
        qsos.append({
            "call": call,
            "band": (r.get("BAND") or "").strip().lower(),
            "mode": (r.get("MODE") or "").strip().upper(),
            "date": qso_date,
            "grid": grid or None,
            "frequency_hz": _adif_freq_to_hz(r.get("FREQ")),
            "lat": lat, "lon": lon,
            "qth_lat": qth_lat, "qth_lon": qth_lon,
            # ADIF's own NAME/COUNTRY fields (when the logging software
            # already captured them) take priority over QRZ's -- more
            # likely to reflect what was actually true at QSO time, and
            # doesn't cost an extra lookup for the common case where
            # GRIDSQUARE already gave us a position.
            "name": (r.get("NAME") or "").strip() or qrz_name, "location": location,
            "city": city, "state": state,
            "country": (r.get("COUNTRY") or "").strip() or qrz_country,
            "rst_sent": (r.get("RST_SENT") or "").strip() or None,
            "rst_rcvd": (r.get("RST_RCVD") or "").strip() or None,
            "logged_at": _adif_datetime_to_epoch(qso_date, r.get("TIME_ON")),
        })

    save_qsos(qsos)
    return jsonify({"ok": True, "count": len(qsos), "filename": filename, "qsos": qsos})

@app.route("/api/clear_qsos", methods=["POST"])
def api_clear_qsos():
    save_qsos([])
    return jsonify({"ok": True})

@app.route("/api/wsjtx_status")
def api_wsjtx_status():
    """Lets Settings show whether the WSJT-X UDP listener is actually
    receiving anything, not just whether the toggle is on -- proof of
    life independent of a QSO ever completing (a Heartbeat updates
    last_packet_at too, see wsjtx.py)."""
    return jsonify(wsjtx_listener.status())

@app.route("/api/quiz_question")
def api_quiz_question():
    """One random question from the bundled Technician/General/Extra pool
    (license_quiz.py, ?class=technician|general|extra, default extra for
    backward compat) -- picked server-side rather than shipping a whole
    ~400-600-question pool to the browser, matching every other card's
    small-payload-per-poll pattern. Per-section accuracy stats are tracked
    client-side in localStorage, not here -- this app has no user
    accounts. An unrecognized class value just yields an empty pool
    (license_quiz.random_question() returns None), same 503 as a missing
    pool file -- no separate validation needed."""
    license_class = request.args.get("class", LICENSE_QUIZ_DEFAULT_CLASS)
    q = license_quiz.random_question(license_class)
    if q is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(q)

@app.route("/api/satellites")
def api_satellites():
    """Current positions (+ ground track, for the Live map overlay) and
    upcoming passes (for the Satellites card) -- see satellites.py.
    Observer position reuses settings.station_grid, same as
    wspr_activity.py/psk_reporter.py; passes are omitted (not a 503)
    when no station_grid is set, since positions/ground-track still work
    without an observer location -- only pass prediction needs one."""
    settings = load_settings()
    tracked = settings.get("tracked_satellites") or []
    positions = satellite_tracker.positions(tracked)
    qth = grid_to_latlon(settings.get("station_grid", ""))
    passes = satellite_tracker.passes(tracked, qth[0], qth[1]) if qth else []
    # Real-time az/el for the polar elevation scope -- a different
    # question than `passes` answers, see sky_snapshot()'s own docstring.
    sky = satellite_tracker.sky_snapshot(tracked, qth[0], qth[1], passes) if qth else None
    return jsonify({"positions": positions, "passes": passes, "has_observer": qth is not None, "sky": sky})

@app.route("/api/starlink_train")
def api_starlink_train():
    """Best-effort possible-Starlink-train sighting data -- see
    starlink_trains.py's own docstring for why this is more fragile
    than every other integration here (an HTML-scraped "current launch
    batch" lookup, no stable API). A separate route from /api/satellites
    on purpose -- this fetch is slower/less reliable, and shouldn't be
    able to slow down or break the regular ham-satellite pass list.
    503 covers "not enabled"/"no station grid"/"no current batch
    tracked"/"fetch failed" alike -- the frontend shows a quiet
    unavailable state for all of these, same degrade-gracefully
    contract as every other integration."""
    settings = load_settings()
    if not settings.get("show_starlink_trains", False):
        return jsonify({"error": "disabled"}), 503
    qth = grid_to_latlon(settings.get("station_grid", ""))
    if not qth:
        return jsonify({"error": "no_station_grid"}), 503
    data = starlink_train_client.visible_passes(qth[0], qth[1])
    if data is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(data)

@app.route("/version")
def version_page():
    embed = request.args.get("embed") == "1"
    return render_template("version.html", settings=load_settings(), embed=embed,
                           app_version=config.APP_VERSION, app_codename=config.APP_CODENAME,
                           codename_bio=get_codename_bio(config.APP_CODENAME))

@app.route("/api/check_for_updates")
def api_check_for_updates():
    """Passive check (cached, ~5min TTL) on page load, or a forced refresh
    from the Version tab's "Check for updates" button (?force=1)."""
    settings = load_settings()
    if not settings.get("update_check_enabled", True):
        return jsonify({"enabled": False})
    result = update_checker.check(
        settings.get("update_check_repo", ""),
        settings.get("update_check_branch", "main"),
        force=request.args.get("force") == "1",
    )
    if result is None:
        return jsonify({"enabled": True, "error": "check_failed"})
    result["enabled"] = True
    result["host_is_standalone"] = HOST_IS_STANDALONE
    return jsonify(result)

@app.route("/api/install_update", methods=["POST"])
def api_install_update():
    """Triggers the standalone updater: writes a flag file that a systemd
    .path unit (installed by install.sh/update.sh) watches for, running
    `git pull` + update.sh as root -- see README.md "Self-update" and
    install.sh's updater-service section. The web app itself never runs
    the pull/restart directly: it's unprivileged (NoNewPrivileges=yes,
    ProtectSystem=strict) and can only write inside CONFIG_DIR, same
    privilege-separation pattern as Host power control's polkit rule."""
    if not HOST_IS_STANDALONE:
        return jsonify({"success": False, "message": "Not available on this deployment"}), 403
    try:
        trigger_path = os.path.join(config.CONFIG_DIR, "update_requested")
        with open(trigger_path, "w") as f:
            f.write(str(time.time()))
        return jsonify({"success": True,
                        "message": "Update started -- the dashboard will restart shortly"})
    except OSError as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/readme")
def readme_page():
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
    except FileNotFoundError:
        readme_text = ("README.md not found alongside the running app -- for a Docker/Unraid "
                        "deployment, check your image build; for a standalone Pi/Linux install, "
                        "re-run install.sh or update.sh to pick it up.")
    embed = request.args.get("embed") == "1"
    return render_template("readme.html", settings=load_settings(), readme_text=readme_text, embed=embed)


# --- hotspot management ---

@app.route("/api/test_ssh", methods=["POST"])
def test_ssh():
    data   = request.json
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            data["ip"], username=data["user"], password=data["pass"],
            timeout=config.SSH_TIMEOUT,
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        client.close()

@app.route("/api/test_asl_node", methods=["POST"])
def test_asl_node():
    """Test an ASL3 hotspot's SSH connectivity and node number for the
    Settings 'Test' button -- connects, runs the same `rpt xnode` command
    check_one will use, and reports what it found. Doubles as a live smoke
    test for the RPT_ALINKS parser."""
    data     = request.json or {}
    ip       = data.get("ip", "").strip()
    user     = data.get("user", "").strip()
    password = data.get("pass", "")
    node     = data.get("asl_node", "").strip()
    if not node.isdigit():
        return jsonify({"success": False, "message": "Node number must be digits only"})
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=config.SSH_TIMEOUT)
        cmd = config.build_asl_status_cmd(node)
        _, stdout, stderr = client.exec_command(cmd, timeout=config.SSH_TIMEOUT)
        stdout_text = stdout.read().decode("utf-8", errors="ignore")
        # Previously discarded entirely -- a real, reported case (node
        # confirmed working when the user ran the exact same `rpt xnode`
        # command by hand over SSH) showed this test still failing with no
        # way to see WHY, since a permission/sudo error on the remote host
        # goes to stderr and this app was never even looking at it.
        stderr_text = stderr.read().decode("utf-8", errors="ignore").strip()
        output = stdout_text.splitlines()
        alinks_raw = None
        for line in output[3:]:
            m = re.match(config.ASL_ALINKS_LINE_PATTERN, line.strip())
            if m:
                alinks_raw = m.group(1)
                break
        if alinks_raw is None:
            detail_parts = []
            if stderr_text:
                detail_parts.append(f"stderr: {stderr_text[:300]}")
            if output:
                detail_parts.append(f"output: {' | '.join(output[:8])}"[:400])
            else:
                detail_parts.append("command produced no output at all")
            return jsonify({
                "success": False,
                "message": (
                    f"Connected, but node {node} didn't return link status — "
                    f"check the node number ({'; '.join(detail_parts)})"
                ),
            })
        count = len(alinks_raw.split(",")[1:])
        return jsonify({
            "success": True,
            "message": f"Connected — node {node} found, {count} linked node{'s' if count != 1 else ''}",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        client.close()


@app.route("/api/test_openspot4", methods=["POST"])
def test_openspot4():
    """Test an openSPOT4 hotspot's admin password(s) for the Settings
    'Test' button -- login + checktok only, no persistent WebSocket
    opened. extra_pass is the raw multi-line textarea value (other
    config profiles' passwords, since each profile can have its own)."""
    data       = request.json or {}
    ip         = data.get("ip", "").strip()
    password   = data.get("pass", "")
    extra_pass = data.get("extra_pass", "")
    ok, message = openspot_manager.test_connection(ip, password, extra_pass)
    return jsonify({"success": ok, "message": message})


@app.route("/api/asl_connect", methods=["POST"])
def api_asl_connect():
    """Connect, monitor (receive-only), local-monitor (receive-only, no
    relay to other links), disconnect one link, or disconnect_all
    (every link on the node at once) on an ASL3 hotspot -- the
    dashboard's "ASL Favorites & Control" card and ASL Control sidebar.
    Uses `rpt cmd <node> ilink <code> <remotenode>` (confirmed against a
    real node, and matches how AllScan
    -- https://github.com/davidgsd/AllScan -- does the same thing), NOT the
    DTMF-simulated `rpt fun <node> *3<remotenode>` form, which requires
    replicating app_rpt's digit-collection state machine and proved
    unreliable in practice."""
    data       = request.json or {}
    hotspot_id = data.get("id", "").strip()
    node       = data.get("node", "").strip()
    action     = data.get("action", "").strip()

    hotspot = next((h for h in load_hotspots() if h.get("id") == hotspot_id), None)
    if hotspot is None or hotspot.get("type") != "asl3":
        return jsonify({"success": False, "message": "Not an ASL3 hotspot"}), 400
    ip = hotspot["ip"]
    local_node = hotspot.get("asl_node", "")
    if not local_node.isdigit():
        return jsonify({"success": False, "message": "Invalid node number"}), 400
    ilink_code = {
        "connect":        config.ASL_ILINK_CONNECT,
        "monitor":        config.ASL_ILINK_MONITOR,
        "localmonitor":   config.ASL_ILINK_LOCAL_MONITOR,
        "disconnect":     config.ASL_ILINK_DISCONNECT,
        "disconnect_all": config.ASL_ILINK_DISCONNECT_ALL,
    }.get(action)
    if ilink_code is None:
        return jsonify({"success": False, "message": "Invalid action"}), 400

    # disconnect_all targets EVERY link on the node (AllScan's own
    # convention: remotenode "0" means "all"), never a caller-supplied
    # node -- hardcoded here rather than trusting whatever the client
    # sent, so a stray/wrong node value in the request can never turn
    # this into a targeted disconnect of the wrong link.
    if action == "disconnect_all":
        node = "0"
    elif not node.isdigit():
        return jsonify({"success": False, "message": "Invalid node number"}), 400

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=hotspot["user"], password=hotspot["pass"], timeout=config.SSH_TIMEOUT)
        cmd = config.build_asl_ilink_cmd(local_node, ilink_code, node)
        _, stdout, _ = client.exec_command(cmd, timeout=config.SSH_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="ignore").strip()
        default_message = "Disconnected all links" if action == "disconnect_all" else f"{action.capitalize()}ed {node}"
        return jsonify({"success": True, "message": output or default_message})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        client.close()


@app.route("/api/brandmeister_talkgroup", methods=["POST"])
def api_brandmeister_talkgroup():
    """Link/unlink a static Brandmeister talkgroup for a hotspot's own
    Brandmeister/CCS7 device -- the hotspot card drawer's "Brandmeister
    Talkgroups" section. Uses the confirmed-live v2 write endpoints
    (see brandmeister.py's module docstring) rather than anything TGIF-
    related -- TGIF has no working API to build against at all (even the
    third-party WPSD-Dashboard project this was inspired by ships its own
    TGIF Manager as non-functional, "does not work until TGIF's API is
    made available")."""
    data       = request.json or {}
    hotspot_id = data.get("id", "").strip()
    action     = data.get("action", "").strip()

    hotspot = next((h for h in load_hotspots() if h.get("id") == hotspot_id), None)
    if hotspot is None:
        return jsonify({"success": False, "message": "Unknown hotspot"}), 400
    bm_id = (hotspot.get("brandmeister_id") or "").strip()
    if not bm_id:
        return jsonify({"success": False, "message": "No Brandmeister ID configured for this hotspot"}), 400

    api_key = (load_settings().get("brandmeister_api_key") or "").strip()
    if not api_key:
        return jsonify({"success": False, "message": "No Brandmeister API key configured (Settings → Integrations)"}), 400

    try:
        tg   = int(data.get("tg"))
        slot = int(data.get("slot"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid talkgroup number"}), 400
    if slot not in (1, 2):
        return jsonify({"success": False, "message": "Slot must be 1 or 2"}), 400

    if action == "link":
        ok, message = bm_write_client.set_static_talkgroup(bm_id, tg, slot, api_key)
    elif action == "unlink":
        ok, message = bm_write_client.remove_static_talkgroup(bm_id, tg, slot, api_key)
    else:
        return jsonify({"success": False, "message": "Invalid action"}), 400

    if ok:
        # Reflect the change immediately rather than waiting up to 30 min
        # for the next run_slow_checks_forever() cycle to re-fetch it --
        # invalidate() (called inside set_/remove_static_talkgroup on
        # success) guarantees this lookup() hits the network, not a stale
        # cached list.
        info = bm_write_client.lookup(bm_id)
        if info is not None:
            monitor.apply_bm_static_tgs(hotspot_id, info["static_talkgroups"])
    return jsonify({"success": ok, "message": message})


@app.route("/api/test_qrz", methods=["POST"])
def test_qrz():
    """Test QRZ credentials by attempting a login and looking up the user's own callsign."""
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "Username and password required"})
    try:
        client = QrzClient(username, password, config.QRZ_AGENT)
        # Look up the user's own callsign — always works with a valid login
        result = client.lookup(username.upper())
        if result:
            return jsonify({"success": True, "message": f"Connected — logged in as {username.upper()}"})
        else:
            return jsonify({"success": False, "message": "Login failed or subscription required for lookups"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/test_aprs", methods=["POST"])
def test_aprs():
    """Test an APRS.fi API key against the aprs.fi API (checks the key itself,
    not whether any particular station has data)."""
    data    = request.json or {}
    api_key = data.get("api_key", "").strip()
    if not api_key:
        return jsonify({"success": False, "message": "API key required"})
    client = AprsClient(api_key)
    ok, message = client.test_key()
    return jsonify({"success": ok, "message": message})


@app.route("/api/test_aprs_msg", methods=["POST"])
def test_aprs_msg():
    """Sends a real one-time test message via APRS-IS for the Settings 'Test' button."""
    data = request.json or {}
    my_callsign = data.get("my_callsign", "").strip()
    to_callsign = data.get("to_callsign", "").strip()
    if not my_callsign:
        return jsonify({"success": False, "message": "Callsign required"})
    ok, message = AprsMessenger.test_connection(my_callsign, to_callsign)
    return jsonify({"success": ok, "message": message})


@app.route("/api/aprs_inbox")
def api_aprs_inbox():
    """Recent APRS-IS messages addressed to your callsign, plus the
    listener connection's live status -- see aprs_inbox.py."""
    status = aprs_inbox.status()
    status["messages"] = aprs_inbox.messages()
    return jsonify(status)

@app.route("/api/fleet_events")
def api_fleet_events():
    """Recent fleet online/offline transitions for the Notifications
    card -- monitor.py's own offline_since tracking, no new poll/
    connection, just an event log of when it already flips."""
    return jsonify({"events": monitor.fleet_events()})

@app.route("/api/hf_alerts")
def api_hf_alerts():
    """Recent geomagnetic-storm (Kp>=5) threshold-crossing events for the
    Notifications card -- hf_conditions.py's own hourly K-index fetch,
    no new poll/connection, just an event log of when it crosses."""
    return jsonify({"events": hf_conditions.alert_events()})

@app.route("/api/brandmeister_lh")
def api_brandmeister_lh():
    """Favorite-callsign activity anywhere on the Brandmeister network
    (not just this fleet's own hotspots) for the Notifications card --
    see brandmeister_lastheard.py."""
    status = brandmeister_lh.status()
    status["events"] = brandmeister_lh.events()
    return jsonify(status)

@app.route("/api/qrz_confirmations")
def api_qrz_confirmations():
    """"Confirmed contact" events for the Notifications card, plus the
    QRZ Logbook sync's own health (for the source pill's connection dot).
    The sync itself runs in _qrz_logbook_loop, not here -- see
    qrz_logbook.py."""
    return jsonify(qrz_logbook_client.status())

@app.route("/api/qrz_logbook_sync", methods=["POST"])
def api_qrz_logbook_sync():
    """Run a QRZ Logbook sync right now instead of waiting for the next
    background cycle -- for the Settings "Sync now" button. Fires in a
    daemon thread and returns immediately; poll /api/qrz_confirmations
    for the result (last_sync / last_error). A sync already in progress
    is a harmless no-op (qrz_logbook.py's own _sync_lock guard)."""
    if not qrz_logbook_client.enabled:
        return jsonify({"ok": False, "reason": "QRZ Logbook sync is off or has no API key"}), 400
    grid = load_settings().get("station_grid", "")
    threading.Thread(
        target=qrz_logbook_client.sync,
        args=(monitor.lookup_caller_info, grid),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/test_hamalert", methods=["POST"])
def test_hamalert():
    """Test a HamAlert Telnet login for the Settings 'Test connection'
    button -- see HamAlertListener.test_connection()."""
    data = request.json or {}
    ok, message = HamAlertListener.test_connection(data.get("username", ""), data.get("password", ""))
    return jsonify({"success": ok, "message": message})


@app.route("/api/hamalert")
def api_hamalert():
    """Recent HamAlert trigger matches, plus the listener connection's
    live status -- see hamalert.py."""
    status = hamalert_listener.status()
    status["alerts"] = hamalert_listener.recent()
    return jsonify(status)


@app.route("/api/digipi")
def api_digipi():
    """DigiPi connection status + recent parsed Direwolf/APRS activity --
    see digipi.py."""
    status = digipi_monitor.status()
    status["packets"] = digipi_monitor.packets()
    return jsonify(status)


@app.route("/api/test_mqtt", methods=["POST"])
def test_mqtt():
    """Test MQTT broker connectivity for the Settings 'Test' button."""
    data = request.json or {}
    host = data.get("host", "").strip()
    if not host:
        return jsonify({"success": False, "message": "Broker host required"})
    try:
        port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        port = 1883
    ok, message = MqttPublisher.test_connection(
        host, port, data.get("username", ""), data.get("password", "")
    )
    return jsonify({"success": ok, "message": message})


@app.route("/api/test_brandmeister", methods=["POST"])
def test_brandmeister():
    """Test a Brandmeister repeater/hotspot ID against the v2 device API.
    Uses debug_lookup() so a bad ID, an HTTP error, or an unexpected response
    shape are distinguishable instead of all collapsing into "not found"."""
    data        = request.json or {}
    repeater_id = data.get("repeater_id", "").strip()
    if not repeater_id:
        return jsonify({"success": False, "message": "Repeater/hotspot ID required"})
    client = BrandmeisterClient()
    debug  = client.debug_lookup(repeater_id)
    if debug["result"] is None:
        return jsonify({"success": False, "message": debug["detail"]})
    r = debug["result"]
    tg_count = len(r["static_talkgroups"])
    return jsonify({
        "success": True,
        "message": f"Found {r['callsign']} — status: {r['status_text'] or 'unknown'}, "
                   f"{tg_count} static talkgroup{'s' if tg_count != 1 else ''}",
    })


@app.route("/api/reorder_hotspots", methods=["POST"])
def reorder_hotspots():
    """Accept an ordered list of hotspot ids and persist that order."""
    ordered_ids = request.json or []
    hotspots    = load_hotspots()
    by_id       = {h["id"]: h for h in hotspots}
    reordered   = [by_id[key] for key in ordered_ids if key in by_id]
    # Append any hotspots not mentioned in the payload (safety net)
    mentioned = set(ordered_ids)
    reordered += [h for h in hotspots if h["id"] not in mentioned]
    save_hotspots(reordered)
    return jsonify({"ok": True})

@app.route("/api/delete_hotspot/<hotspot_id>", methods=["POST"])
def delete_hotspot(hotspot_id):
    save_hotspots([h for h in load_hotspots() if h.get("id") != hotspot_id])
    monitor.remove(hotspot_id)
    openspot_manager.remove(hotspot_id)
    audio_manager.remove(hotspot_id)
    return redirect("/setup")

@app.route("/api/toggle_hotspot/<hotspot_id>", methods=["POST"])
def toggle_hotspot(hotspot_id):
    """Quick on/off from the Settings hotspot list -- config/credentials
    stay in hotspots.json either way, only the enabled flag flips. Same
    mutate-save-reconcile shape as /setup's POST handler. Matched by id
    (not ip) so toggling one of two same-ip hotspots (e.g. two ASL3
    radios on one box) doesn't flip the other one too."""
    hotspots = load_hotspots()
    for h in hotspots:
        if h.get("id") == hotspot_id:
            h["enabled"] = not h.get("enabled", True)
    save_hotspots(hotspots)
    if mqtt_pub.enabled:
        mqtt_pub.set_hotspots(hotspots)
    openspot_manager.reconcile(hotspots)
    audio_manager.reconcile(hotspots)
    # If just disabled, drop it from the live snapshot immediately rather
    # than waiting for the next poll tick's prune_stale to catch up.
    updated = next((h for h in hotspots if h.get("id") == hotspot_id), None)
    if updated is not None and not updated.get("enabled", True):
        monitor.remove(hotspot_id)
    return redirect("/setup")


# --- cameras (RTSP / Wyze RTSP firmware / Bambu Labs A1) ---
# "wyze" is a cosmetic alias for "rtsp" -- same rtsp_url field, same
# ffmpeg-based _RtspWorker in camera_stream.py (its dispatch already
# treats anything other than "bambu_a1" as RTSP). Wyze cameras with
# Wyze's own official RTSP firmware installed just speak plain RTSP;
# this only exists so the Settings dropdown/badge says "Wyze" instead
# of a generic "RTSP" for people who don't know their camera can do
# this at all.
# One card per camera on the dashboard, same as hotspot cards -- positioned
# via the same Card order drag list, using the sentinel-position scheme
# (camera.position = "after the Nth hotspot card") rather than hotspots.json's
# own plain list-order scheme, since cameras need to interleave freely among
# hotspot cards rather than only reordering relative to each other.

@app.route("/api/cameras", methods=["GET"])
def api_cameras_get():
    return jsonify(load_cameras())

@app.route("/api/cameras", methods=["POST"])
def api_cameras_post():
    """Add or update one camera. An existing camera (matched by id) has its
    stream worker torn down after saving, so a config change (e.g. a
    corrected RTSP URL or access code) takes effect on the next view
    instead of the worker silently continuing to use stale settings."""
    data     = request.json or {}
    name     = data.get("name", "").strip()
    cam_type = data.get("type", "").strip()
    if not name or cam_type not in ("rtsp", "wyze", "bambu_a1"):
        return jsonify({"ok": False, "message": "Name and a valid camera type are required"}), 400

    cameras   = load_cameras()
    camera_id = data.get("id", "").strip()
    existing  = next((c for c in cameras if c["id"] == camera_id), None) if camera_id else None

    camera = dict(existing) if existing else {
        "id": camera_id or f"cam-{uuid.uuid4().hex[:10]}",
        "position": len(cameras),
    }
    camera["name"] = name
    camera["type"] = cam_type
    if cam_type in ("rtsp", "wyze"):
        camera["rtsp_url"] = data.get("rtsp_url", "").strip()
        camera.pop("ip", None)
        camera.pop("serial", None)
        camera.pop("access_code", None)
    else:
        camera["ip"]          = data.get("ip", "").strip()
        camera["serial"]      = data.get("serial", "").strip()
        camera["access_code"] = data.get("access_code", "").strip()
        camera.pop("rtsp_url", None)

    cameras = [c for c in cameras if c["id"] != camera["id"]]
    cameras.append(camera)
    save_cameras(cameras)
    camera_manager.remove(camera["id"])
    return jsonify({"ok": True, "camera": camera})

@app.route("/api/delete_camera/<camera_id>", methods=["POST"])
def api_delete_camera(camera_id):
    save_cameras([c for c in load_cameras() if c["id"] != camera_id])
    camera_manager.remove(camera_id)
    return jsonify({"ok": True})

@app.route("/api/reorder_cameras", methods=["POST"])
def api_reorder_cameras():
    """Accepts {camera_id: position, ...} -- a numeric position among the
    hotspot cards for each camera (see module note above), not an ordered
    list of ids the way /api/reorder_hotspots works."""
    positions = request.json or {}
    cameras = load_cameras()
    for camera in cameras:
        if camera["id"] in positions:
            try:
                camera["position"] = max(0, int(positions[camera["id"]]))
            except (TypeError, ValueError):
                pass
    save_cameras(cameras)
    return jsonify({"ok": True})

@app.route("/api/test_camera", methods=["POST"])
def api_test_camera():
    """Grabs one real frame with a hard timeout for the Settings 'Test
    connection' button -- doubles as a smoke test for both the RTSP/ffmpeg
    and Bambu/bambulabs_api paths, reusing the exact same worker code the
    live dashboard card uses rather than a separate, possibly-divergent
    test-only implementation."""
    data     = request.json or {}
    cam_type = data.get("type", "").strip()
    test_id  = f"__test__{uuid.uuid4().hex[:8]}"
    camera   = {**data, "id": test_id, "type": cam_type}

    result = {"success": False, "message": "Unknown camera type"}
    done   = threading.Event()

    def worker():
        nonlocal result
        try:
            gen = camera_manager.stream(camera)
            for _chunk in gen:
                result = {"success": True, "message": "Connected — frame received"}
                gen.close()
                break
            else:
                status = camera_manager.status(camera)
                result = {"success": False, "message": status.get("detail") or "No frame received"}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    done.wait(config.CAMERA_TEST_TIMEOUT)
    if not done.is_set():
        result = {"success": False, "message": "Timed out waiting for a frame"}
    camera_manager.remove(test_id)
    return jsonify(result)

@app.route("/api/camera_feed/<camera_id>")
def api_camera_feed(camera_id):
    camera = next((c for c in load_cameras() if c["id"] == camera_id), None)
    if camera is None:
        return jsonify({"error": "not found"}), 404
    return Response(camera_manager.stream(camera),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/camera_status")
def api_camera_status():
    return jsonify({c["id"]: camera_manager.status(c) for c in load_cameras()})


# --- backup / restore ---
# One combined export covering every config category (hotspots, favorites,
# asl_favorites, cameras, settings) instead of five separate downloads --
# this is a thin bundling/merging layer over storage.py's existing load_*/
# save_* functions, no new storage format. Import supports "merge" (add new,
# update existing by the same identity field each list already uses --
# hotspot ip, favorite call, asl_favorite node, camera id -- leave everything
# else untouched) or "replace" (each checked category becomes exactly what's
# in the file). Settings has no real "identity" to merge by -- merge there
# just means "overwrite the keys present in the file, leave the rest," which
# load_settings() already does implicitly on every read via its own merge
# onto DEFAULT_SETTINGS.
@app.route("/api/export_backup")
def api_export_backup():
    requested = set(
        c.strip() for c in
        (request.args.get("categories") or "hotspots,favorites,asl_favorites,bm_tg_favorites,cameras,settings").split(",")
        if c.strip()
    )
    backup = {
        "exported_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    if "hotspots" in requested:
        backup["hotspots"] = load_hotspots()
    if "favorites" in requested:
        backup["favorites"] = load_favorites()
    if "asl_favorites" in requested:
        backup["asl_favorites"] = load_asl_favorites()
    if "bm_tg_favorites" in requested:
        backup["bm_tg_favorites"] = load_bm_tg_favorites()
    if "cameras" in requested:
        backup["cameras"] = load_cameras()
    if "settings" in requested:
        backup["settings"] = load_settings()

    filename = f"dashboard-backup-{datetime.date.today().isoformat()}.json"
    resp = jsonify(backup)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/api/import_backup", methods=["POST"])
def api_import_backup():
    data = request.json or {}
    mode = data.get("mode", "merge")
    if mode not in ("merge", "replace"):
        return jsonify({"ok": False, "message": "Invalid mode"}), 400

    result = {}

    if isinstance(data.get("hotspots"), list):
        imported = []
        for h in data["hotspots"]:
            if not isinstance(h, dict) or not h.get("name") or not h.get("ip"):
                continue
            h = dict(h)
            # Interpolated into a shell string over SSH (config.build_asl_status_cmd)
            # -- validate digits-only here too, same requirement /setup already
            # enforces, since an imported file is exactly as untrusted as a form
            # submission would be (no auth on this endpoint either).
            asl_node = str(h.get("asl_node", "")).strip()
            if asl_node and not asl_node.isdigit():
                h.pop("asl_node", None)
            # Same re-validation for DVSwitch card ports -- also
            # shell-interpolated over SSH (config.build_asl_status_cmd).
            raw_ports = str(h.get("dvswitch_ports", "")).strip()
            if raw_ports:
                ports = [p.strip() for p in re.split(r"[,\n]+", raw_ports) if p.strip().isdigit()]
                if ports:
                    h["dvswitch_ports"] = ",".join(ports)
                else:
                    h.pop("dvswitch_ports", None)
            # Re-validated the same way as /setup and /api/update_hotspot --
            # not shell-interpolated like the fields above, but rendered
            # into an href attribute client-side, so a "javascript:" value
            # from an untrusted import file needs to be screened out here too.
            card_url = str(h.get("card_url", "")).strip()
            if card_url and not re.match(r"^https?://", card_url, re.IGNORECASE):
                h.pop("card_url", None)
            # Same re-validation for the sa818-menu config path override --
            # shell-interpolated over SSH (config.build_sa818_conf_cmd).
            sa818_conf_path = str(h.get("sa818_conf_path", "")).strip()
            if sa818_conf_path and not re.match(config.SA818_CONF_PATH_PATTERN, sa818_conf_path):
                h.pop("sa818_conf_path", None)
            h["audio_meter_enabled"] = bool(h.get("audio_meter_enabled", False))
            # Generic across every hotspot type -- see /setup's own comment.
            h["portable"] = bool(h.get("portable", False))
            imported.append(h)
        if mode == "replace":
            # A replace-mode import still needs every hotspot to have an id
            # -- load_hotspots() would backfill it on the NEXT load, but
            # this list is used immediately below (mqtt_pub.set_hotspots,
            # openspot_manager.reconcile, audio_manager.reconcile all read
            # hotspot["id"]), so backfill inline instead of waiting.
            existing_by_ip = {h["ip"]: h for h in load_hotspots()}
            for h in imported:
                if not h.get("id"):
                    h["id"] = (existing_by_ip.get(h["ip"], {}).get("id")
                               or f"hs-{uuid.uuid4().hex[:10]}")
            hotspots = imported
        else:
            # Merge by id -- ip is no longer a unique identity (two
            # hotspots can share one, e.g. two ASL3 radios on one box).
            # An imported entry with its own id merges onto the matching
            # existing entry; one without an id (an older, pre-migration
            # backup file) falls back to matching by ip so a restore from
            # an old export still updates in place rather than duplicating.
            current = load_hotspots()
            by_id   = {h["id"]: h for h in current}
            by_ip   = {h["ip"]: h["id"] for h in current}
            for h in imported:
                key = h.get("id") or by_ip.get(h["ip"]) or f"hs-{uuid.uuid4().hex[:10]}"
                h["id"] = key
                by_id[key] = h
            hotspots = list(by_id.values())
        save_hotspots(hotspots)
        result["hotspots"] = len(hotspots)
        if mqtt_pub.enabled:
            mqtt_pub.set_hotspots(hotspots)
        openspot_manager.reconcile(hotspots)
        audio_manager.reconcile(hotspots)

    if isinstance(data.get("favorites"), list):
        imported = [
            {"call": f["call"].upper().strip(), "label": f.get("label", "").strip()}
            for f in data["favorites"] if isinstance(f, dict) and f.get("call", "").strip()
        ]
        if mode == "replace":
            favorites = imported
        else:
            by_call = {f["call"]: f for f in load_favorites()}
            for f in imported:
                by_call[f["call"]] = f
            favorites = list(by_call.values())
        save_favorites(favorites)
        result["favorites"] = len(favorites)

    if isinstance(data.get("asl_favorites"), list):
        imported = [
            {"node": f["node"].strip(), "label": f.get("label", "").strip()}
            for f in data["asl_favorites"] if isinstance(f, dict) and f.get("node", "").strip().isdigit()
        ]
        if mode == "replace":
            asl_favorites = imported
        else:
            by_node = {f["node"]: f for f in load_asl_favorites()}
            for f in imported:
                by_node[f["node"]] = f
            asl_favorites = list(by_node.values())
        save_asl_favorites(asl_favorites)
        result["asl_favorites"] = len(asl_favorites)

    if isinstance(data.get("bm_tg_favorites"), list):
        imported = [
            {"tg": str(f.get("tg", "")).strip(), "slot": str(f.get("slot", "")).strip(), "label": f.get("label", "").strip()}
            for f in data["bm_tg_favorites"]
            if isinstance(f, dict) and str(f.get("tg", "")).strip().isdigit() and str(f.get("slot", "")).strip() in ("1", "2")
        ]
        if mode == "replace":
            bm_tg_favorites = imported
        else:
            # Keyed by (tg, slot) together -- the same TG can legitimately
            # be a separate favorite on TS1 and TS2, unlike ASL favorites'
            # plain node-number key above.
            by_key = {(f["tg"], f["slot"]): f for f in load_bm_tg_favorites()}
            for f in imported:
                by_key[(f["tg"], f["slot"])] = f
            bm_tg_favorites = list(by_key.values())
        save_bm_tg_favorites(bm_tg_favorites)
        result["bm_tg_favorites"] = len(bm_tg_favorites)

    if isinstance(data.get("cameras"), list):
        imported = [
            c for c in data["cameras"]
            if isinstance(c, dict) and c.get("id") and c.get("name") and c.get("type") in ("rtsp", "wyze", "bambu_a1")
        ]
        if mode == "replace":
            cameras = imported
        else:
            by_id = {c["id"]: c for c in load_cameras()}
            for c in imported:
                by_id[c["id"]] = c
            cameras = list(by_id.values())
        save_cameras(cameras)
        result["cameras"] = len(cameras)
        for c in imported:
            camera_manager.remove(c["id"])  # drop any live worker so it reconnects with the (possibly new) config

    if isinstance(data.get("settings"), dict):
        if mode == "replace":
            new_settings = data["settings"]
        else:
            new_settings = load_settings()
            new_settings.update(data["settings"])
        save_settings(new_settings)
        result["settings"] = True
        # Rebuild every settings-dependent client, same as /api/settings does
        # on every save -- an imported settings block can change any of these.
        _rebuild_qrz_client()
        monitor.set_radioid_enabled(new_settings.get("radioid_enabled", True))
        _rebuild_aprs_client()
        _rebuild_mqtt_client()
        _rebuild_aprs_messenger()
        _rebuild_aprs_inbox()
        _rebuild_wsjtx()
        _rebuild_hamalert()
        _rebuild_brandmeister_lh()
        _rebuild_qrz_logbook()

    return jsonify({"ok": True, "result": result})


def _mqtt_publish_loop():
    """Publishes current hotspot state to MQTT on the same cadence as the
    main poll loop. Decoupled from FleetMonitor itself -- just reads its
    snapshot() the same way /api/data does."""
    while True:
        if mqtt_pub.enabled:
            for entry in monitor.snapshot().values():
                mqtt_pub.publish_state(entry)
        time.sleep(config.MQTT_PUBLISH_INTERVAL)


def _aprs_alert_loop():
    """Checks every hotspot's snapshot each poll cycle and lets AprsMessenger
    decide whether a favorite-active alert should fire (rising edge +
    cooldown, handled inside check_and_send)."""
    while True:
        if aprs_msg.enabled:
            for entry in monitor.snapshot().values():
                aprs_msg.check_and_send(entry)
        time.sleep(config.POLL_INTERVAL)


def _qrz_logbook_loop():
    """Pulls newly-logged QSOs from QRZ Logbook into qsos.json on a slow
    cadence, and (less often) re-checks unconfirmed ones for a
    confirmation flip -- both handled inside QrzLogbookClient.sync(),
    which never raises. Separate background thread, decoupled from the
    poll loop, same pattern as _aprs_alert_loop / _mqtt_publish_loop."""
    time.sleep(15)  # let startup settle before the first network round-trip
    while True:
        try:
            if qrz_logbook_client.enabled:
                qrz_logbook_client.sync(
                    monitor.lookup_caller_info,
                    load_settings().get("station_grid", ""),
                )
        except Exception as e:  # noqa: BLE001 -- sync() already swallows its own; belt and braces
            print(f"[qrz_logbook] loop error: {type(e).__name__}: {e}", flush=True)
        time.sleep(config.QRZ_LOGBOOK_SYNC_INTERVAL)


def main():
    threading.Thread(target=monitor.run_forever, daemon=True).start()
    threading.Thread(target=monitor.run_slow_checks_forever, daemon=True).start()
    threading.Thread(target=_mqtt_publish_loop, daemon=True).start()
    threading.Thread(target=_aprs_alert_loop, daemon=True).start()
    threading.Thread(target=_qrz_logbook_loop, daemon=True).start()
    threading.Thread(target=digipi_monitor.run_forever, daemon=True).start()
    threading.Thread(target=openspot_manager.run_forever, daemon=True).start()
    threading.Thread(target=audio_manager.run_forever, daemon=True).start()
    # waitress, not Flask's own dev server -- see CLAUDE.md gotcha on why
    # this must stay a single process (no --workers-style forking): the
    # FleetMonitor/camera/APRS-inbox background threads started above are
    # all in-process singletons, and a second process would duplicate every
    # SSH poll, ffmpeg camera worker, and APRS-IS login.
    waitress.serve(app, host=config.HOST, port=config.PORT, threads=config.WAITRESS_THREADS)


if __name__ == "__main__":
    main()
