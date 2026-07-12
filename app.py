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
from flask import Flask, jsonify, render_template, request, redirect, Response

import config
import models
import storage as storage_mod
import monitor as monitor_mod
import qrz as qrz_mod
from monitor import FleetMonitor
from storage import load_hotspots, save_hotspots, load_settings, save_settings, \
                   load_favorites, save_favorites, load_asl_favorites, save_asl_favorites, \
                   load_cameras, save_cameras
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
from license_quiz import LicenseQuizPool
from wspr_activity import WsprActivityClient

import host_stats as host_stats_mod

app        = Flask(__name__)
monitor    = FleetMonitor()
wx         = WeatherClient()
host_stats = HostStats()
host_stats.start()
update_checker = UpdateChecker()
hf_conditions   = HfConditionsClient()
camera_manager  = CameraStreamManager()
license_quiz    = LicenseQuizPool()
wspr_activity   = WsprActivityClient()

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
    })

@app.route("/api/data")
def api_data():
    snap      = monitor.snapshot()
    hotspots  = load_hotspots()
    by_ip     = {h["ip"]: h for h in hotspots}
    for ip, entry in snap.items():
        hs = by_ip.get(ip, {})
        entry["lat"]  = hs.get("lat")
        entry["lon"]  = hs.get("lon")
        entry["type"] = hs.get("type", "wpsd")
    ordered_ips = [h["ip"] for h in hotspots]
    # Return as an ARRAY so the browser preserves order — JS objects keyed by
    # IP strings get silently re-sorted by some engines (especially for
    # keys that look numeric), so a dict is not reliable here.
    # Only ever return entries for IPs currently in load_hotspots() -- never
    # fall back to "whatever is in the snapshot," since an in-flight poll
    # thread can resurrect a just-deleted hotspot's entry in monitor._data
    # (check_one -> _ensure_entry recreates it before the SSH call
    # finishes), which would otherwise make a deleted card reappear.
    ordered = [snap[ip] for ip in ordered_ips if ip in snap]
    return jsonify(ordered)

@app.route("/api/map_data")
def api_map_data():
    return jsonify(monitor.map_data())

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data     = request.json or {}
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
    if "aprs_inbox_enabled" in data:
        settings["aprs_inbox_enabled"] = bool(data["aprs_inbox_enabled"])
    if "aprs_inbox_position" in data:
        try:
            settings["aprs_inbox_position"] = max(0, int(data["aprs_inbox_position"]))
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
    save_settings(settings)
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
    return jsonify({"ok": True})


# --- pages ---

@app.route("/")
def dashboard():
    return render_template("dashboard.html", settings=load_settings())

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
    distinct from the callsign favorites above."""
    data = request.json or []
    cleaned = [
        {"node": f["node"].strip(), "label": f.get("label", "").strip()}
        for f in data if f.get("node", "").strip().isdigit()
    ]
    save_asl_favorites(cleaned)
    return jsonify({"ok": True})

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
        new_hotspot = {
            "name": name,
            "ip":   ip,
            "user": request.form.get("user"),
            "pass": request.form.get("pass"),
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
        hotspots = [h for h in hotspots if h["ip"] != new_hotspot["ip"]]
        hotspots.append(new_hotspot)
        save_hotspots(hotspots)
        if mqtt_pub.enabled:
            mqtt_pub.set_hotspots(hotspots)
        return redirect("/setup")
    return render_template("setup.html", hotspots=load_hotspots(),
                           settings=load_settings(), favorites=load_favorites(),
                           cameras=load_cameras(),
                           can_power_control=HOST_CAN_POWER_CONTROL,
                           host_is_standalone=HOST_IS_STANDALONE)

@app.route("/api/host_stats")
def api_host_stats():
    return jsonify(host_stats.snapshot())

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
    result   = storage_activity.query_activity(hours=hours, interval_minutes=interval_minutes)
    hotspots = load_hotspots()
    snap     = monitor.snapshot()
    hotspots_online = sum(
        1 for h in hotspots if snap.get(h["ip"], {}).get("status") != "Offline"
    )
    return jsonify({
        "buckets":                 result["buckets"],
        "mode_breakdown":          result["mode_breakdown"],
        "hotspots_online":         hotspots_online,
        "hotspots_total":          len(hotspots),
        "last_activity":           result["last_activity"],
        "dashboard_uptime_seconds": time.time() - START_TIME,
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

@app.route("/api/quiz_question")
def api_quiz_question():
    """One random question from the bundled Extra pool (license_quiz.py)
    -- picked server-side rather than shipping the whole ~570-question
    pool to the browser, matching every other card's small-payload-per-
    poll pattern. Per-section accuracy stats are tracked client-side in
    localStorage, not here -- this app has no user accounts."""
    q = license_quiz.random_question()
    if q is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(q)

@app.route("/version")
def version_page():
    embed = request.args.get("embed") == "1"
    return render_template("version.html", settings=load_settings(), embed=embed)

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
        readme_text = "README.md not found in the container -- check your image build."
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
        _, stdout, _ = client.exec_command(cmd, timeout=config.SSH_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="ignore").splitlines()
        alinks_raw = None
        for line in output[3:]:
            m = re.match(config.ASL_ALINKS_LINE_PATTERN, line.strip())
            if m:
                alinks_raw = m.group(1)
                break
        if alinks_raw is None:
            return jsonify({
                "success": False,
                "message": f"Connected, but node {node} didn't return link status — check the node number",
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


@app.route("/api/asl_connect", methods=["POST"])
def api_asl_connect():
    """Connect or disconnect a link on an ASL3 hotspot -- the dashboard's
    "ASL Favorites & Control" card. Uses `rpt cmd <node> ilink <code>
    <remotenode>` (confirmed against a real node, and matches how AllScan
    -- https://github.com/davidgsd/AllScan -- does the same thing), NOT the
    DTMF-simulated `rpt fun <node> *3<remotenode>` form, which requires
    replicating app_rpt's digit-collection state machine and proved
    unreliable in practice."""
    data   = request.json or {}
    ip     = data.get("ip", "").strip()
    node   = data.get("node", "").strip()
    action = data.get("action", "").strip()

    hotspot = next((h for h in load_hotspots() if h["ip"] == ip), None)
    if hotspot is None or hotspot.get("type") != "asl3":
        return jsonify({"success": False, "message": "Not an ASL3 hotspot"}), 400
    local_node = hotspot.get("asl_node", "")
    if not local_node.isdigit() or not node.isdigit():
        return jsonify({"success": False, "message": "Invalid node number"}), 400
    ilink_code = {
        "connect":    config.ASL_ILINK_CONNECT,
        "disconnect": config.ASL_ILINK_DISCONNECT,
    }.get(action)
    if ilink_code is None:
        return jsonify({"success": False, "message": "Invalid action"}), 400

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=hotspot["user"], password=hotspot["pass"], timeout=config.SSH_TIMEOUT)
        cmd = config.build_asl_ilink_cmd(local_node, ilink_code, node)
        _, stdout, _ = client.exec_command(cmd, timeout=config.SSH_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="ignore").strip()
        return jsonify({"success": True, "message": output or f"{action.capitalize()}ed {node}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        client.close()


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
    """Accept an ordered list of IPs and persist that order."""
    ordered_ips = request.json or []
    hotspots    = load_hotspots()
    by_ip       = {h["ip"]: h for h in hotspots}
    reordered   = [by_ip[ip] for ip in ordered_ips if ip in by_ip]
    # Append any hotspots not mentioned in the payload (safety net)
    mentioned = set(ordered_ips)
    reordered += [h for h in hotspots if h["ip"] not in mentioned]
    save_hotspots(reordered)
    return jsonify({"ok": True})

@app.route("/api/delete_hotspot/<ip>", methods=["POST"])
def delete_hotspot(ip):
    save_hotspots([h for h in load_hotspots() if h["ip"] != ip])
    monitor.remove(ip)
    return redirect("/setup")


# --- cameras (RTSP / Bambu Labs A1) ---
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
    if not name or cam_type not in ("rtsp", "bambu_a1"):
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
    if cam_type == "rtsp":
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


def main():
    threading.Thread(target=monitor.run_forever, daemon=True).start()
    threading.Thread(target=monitor.run_slow_checks_forever, daemon=True).start()
    threading.Thread(target=_mqtt_publish_loop, daemon=True).start()
    threading.Thread(target=_aprs_alert_loop, daemon=True).start()
    app.run(host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
