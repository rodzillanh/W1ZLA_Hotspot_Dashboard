"""Routes only — polling lives in monitor.py, persistence in storage.py."""
import threading
import hashlib
import inspect
import datetime
import os
import time

import paramiko
from flask import Flask, jsonify, render_template, request, redirect

import config
import models
import storage as storage_mod
import monitor as monitor_mod
import qrz as qrz_mod
from monitor import FleetMonitor
from storage import load_hotspots, save_hotspots, load_settings, save_settings, \
                   load_favorites, save_favorites
from weather import WeatherClient
from qrz import QrzClient
from aprs import AprsClient
from brandmeister import BrandmeisterClient
from mqtt_publisher import MqttPublisher
from aprs_messaging import AprsMessenger
from host_stats import HostStats
import storage_activity

import host_stats as host_stats_mod

app        = Flask(__name__)
monitor    = FleetMonitor()
wx         = WeatherClient()
host_stats = HostStats()
host_stats.start()

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
        entry["lat"] = hs.get("lat")
        entry["lon"] = hs.get("lon")
    ordered_ips = [h["ip"] for h in hotspots]
    # Return as an ARRAY so the browser preserves order — JS objects keyed by
    # IP strings get silently re-sorted by some engines (especially for
    # keys that look numeric), so a dict is not reliable here.
    ordered = [snap[ip] for ip in ordered_ips if ip in snap]
    ordered += [v for ip, v in snap.items() if ip not in set(ordered_ips)]
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

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        hotspots    = load_hotspots()
        new_hotspot = {
            "name": request.form.get("name"),
            "ip":   request.form.get("ip"),
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
        hotspots = [h for h in hotspots if h["ip"] != new_hotspot["ip"]]
        hotspots.append(new_hotspot)
        save_hotspots(hotspots)
        if mqtt_pub.enabled:
            mqtt_pub.set_hotspots(hotspots)
        return redirect("/setup")
    return render_template("setup.html", hotspots=load_hotspots(),
                           settings=load_settings(), favorites=load_favorites())

@app.route("/api/host_stats")
def api_host_stats():
    return jsonify(host_stats.snapshot())

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

@app.route("/version")
def version_page():
    return render_template("version.html", settings=load_settings())


@app.route("/readme")
def readme_page():
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
    except FileNotFoundError:
        readme_text = "README.md not found in the container -- check your image build."
    return render_template("readme.html", settings=load_settings(), readme_text=readme_text)


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
