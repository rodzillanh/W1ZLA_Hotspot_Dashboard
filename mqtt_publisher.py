"""Home Assistant MQTT auto-discovery publisher.

Publishes each hotspot as an HA "device" grouping several entities
(binary_sensor for online/active/favorite-active, sensor for mode,
callsign, temperature, CPU, RSSI, Digital Voice Network Status
reflectors/talkgroups per enabled mode, etc.), using Home Assistant's
MQTT Discovery convention:
https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
Entities appear automatically in HA with no manual YAML configuration --
just point this at the same broker your HA instance already uses.

A SECOND, non-per-hotspot device is also published for the Rig Panel
(PTT/frequency/mode/band/power, backed by rig_panel.py's RigPanelPoller)
-- the single most common ham-shack HA automation trigger is a PTT
binary_sensor driving an on-air light / muting room speakers. Gated on
`rig_control_enabled` AND this client being enabled (set_rig_enabled(),
called from app.py's _rebuild_rig_panel()) rather than a separate opt-in
setting -- turning MQTT on is already the explicit "I want this in HA"
signal, so no extra checkbox was added for it.

Requires the `paho-mqtt` package (see requirements.txt) and a reachable
broker. Everything here is best-effort: a missing/unreachable broker
degrades silently rather than affecting the rest of the app, the same way
a missing QRZ subscription does.
"""
import json
import re
import threading
import time
from typing import Optional

import paho.mqtt.client as mqtt

DISCOVERY_PREFIX    = "homeassistant"
STATE_PREFIX        = "hotspot_dashboard"
AVAILABILITY_TOPIC  = f"{STATE_PREFIX}/status"


def _node_id(hotspot_id: str) -> str:
    # hotspot_id (storage.load_hotspots()'s generated "hs-<hex>") is already
    # a safe HA entity-id string -- the sanitize is defensive, not load-
    # bearing, now that this is keyed by id instead of ip (two hotspots can
    # share an ip, e.g. two ASL3 radios on one box, but never an id).
    return "hotspot_" + re.sub(r"[^A-Za-z0-9_]", "_", hotspot_id)


def _extract_float(s: Optional[str]):
    """'42.5°C' -> 42.5, '-95 dBm' -> -95.0, 'N/A' -> None."""
    if not s:
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _entity_list(hotspot: dict) -> list:
    """(component, object_id, discovery-config-extra-fields) for one hotspot."""
    entities = [
        ("binary_sensor", "online", {
            "name": "Online", "device_class": "connectivity",
            "value_template": "{{ 'ON' if value_json.online else 'OFF' }}",
        }),
        ("binary_sensor", "active", {
            "name": "Active",
            "value_template": "{{ 'ON' if value_json.is_active else 'OFF' }}",
        }),
        ("binary_sensor", "favorite_active", {
            "name": "Favorite Active",
            "value_template": "{{ 'ON' if value_json.favorite_active else 'OFF' }}",
        }),
        ("sensor", "mode", {
            "name": "Mode", "value_template": "{{ value_json.mode }}",
        }),
        ("sensor", "active_call", {
            "name": "Active Call", "value_template": "{{ value_json.active_call }}",
        }),
        ("sensor", "talkgroup", {
            "name": "Talkgroup", "value_template": "{{ value_json.talkgroup }}",
        }),
        ("sensor", "temperature", {
            "name": "Temperature", "device_class": "temperature",
            "unit_of_measurement": "°C", "state_class": "measurement",
            "value_template": "{{ value_json.temperature_c }}",
        }),
        ("sensor", "cpu_load", {
            "name": "CPU Load", "unit_of_measurement": "%", "state_class": "measurement",
            "value_template": "{{ value_json.cpu_pct }}",
        }),
        ("sensor", "rssi", {
            "name": "RSSI", "device_class": "signal_strength",
            "unit_of_measurement": "dBm", "state_class": "measurement",
            "value_template": "{{ value_json.rssi_dbm }}",
        }),
        ("binary_sensor", "update_available", {
            "name": "Dashboard Update Available",
            "value_template": "{{ 'ON' if value_json.dashboard_update_available else 'OFF' }}",
        }),
    ]
    if hotspot.get("brandmeister_id"):
        entities.append(("sensor", "brandmeister_status", {
            "name": "Brandmeister Status",
            "value_template": "{{ value_json.bm_status_text }}",
        }))
        entities.append(("sensor", "dmr_static_tgs", {
            "name": "DMR Static Talkgroups",
            "value_template": "{{ value_json.dmr_static_tgs }}",
        }))
    if hotspot.get("type") == "asl3":
        entities.append(("sensor", "linked_nodes_count", {
            "name": "Linked Nodes", "state_class": "measurement",
            "value_template": "{{ value_json.linked_nodes_count }}",
        }))
    # Digital Voice Network Status (v4.57) -- same per-mode opt-in flags
    # that card/drawer feature already uses, WPSD-only. One sensor per
    # enabled mode showing its current reflector/talkgroup, so an HA
    # automation can react to a link change (e.g. notify when a hotspot
    # drops off a net's reflector).
    if hotspot.get("ircddb_enabled"):
        entities.append(("sensor", "dstar_reflector", {
            "name": "D-STAR Reflector",
            "value_template": "{{ value_json.dstar_reflector }}",
        }))
    if hotspot.get("ysf_status_enabled"):
        entities.append(("sensor", "ysf_reflector", {
            "name": "YSF Reflector",
            "value_template": "{{ value_json.ysf_reflector }}",
        }))
    if hotspot.get("p25_status_enabled"):
        entities.append(("sensor", "p25_talkgroup", {
            "name": "P25 Talkgroup",
            "value_template": "{{ value_json.p25_talkgroup }}",
        }))
    if hotspot.get("nxdn_status_enabled"):
        entities.append(("sensor", "nxdn_talkgroup", {
            "name": "NXDN Talkgroup",
            "value_template": "{{ value_json.nxdn_talkgroup }}",
        }))
    return entities


RIG_NODE_ID    = "rig_panel"
RIG_STATE_TOPIC = f"{STATE_PREFIX}/{RIG_NODE_ID}/state"


def _rig_entity_list() -> list:
    """One global (non-per-hotspot) HA device for the Rig Panel's live
    PTT/frequency/mode/power -- backed by rig_panel.py's RigPanelPoller,
    same source as the Rig Panel card and the QRZ Quick Log card. A PTT
    binary_sensor is the single most common ham-shack HA automation
    trigger (on-air light, mute room speakers, etc.)."""
    return [
        ("binary_sensor", "ptt", {
            "name": "PTT (On Air)",
            "value_template": "{{ 'ON' if value_json.ptt else 'OFF' }}",
        }),
        ("sensor", "frequency", {
            "name": "Frequency", "device_class": "frequency",
            "unit_of_measurement": "MHz", "state_class": "measurement",
            "value_template": "{{ value_json.freq_mhz }}",
        }),
        ("sensor", "mode", {
            "name": "Mode", "value_template": "{{ value_json.mode }}",
        }),
        ("sensor", "band", {
            "name": "Band", "value_template": "{{ value_json.band }}",
        }),
        ("sensor", "power", {
            "name": "Power", "device_class": "power",
            "unit_of_measurement": "W", "state_class": "measurement",
            "value_template": "{{ value_json.power_w }}",
        }),
    ]


class MqttPublisher:
    """Hot-swappable, best-effort MQTT publisher."""

    def __init__(self, host: str = "", port: int = 1883, username: str = "", password: str = ""):
        self.host     = (host or "").strip()
        self.port     = port or 1883
        self.username = (username or "").strip()
        self.password = password or ""
        self._client: Optional[mqtt.Client] = None
        self._lock       = threading.Lock()
        self._hotspots    = []  # last-known hotspot list, for re-announce on reconnect
        self._rig_enabled = False  # set via set_rig_enabled(), for re-announce on reconnect

        if self.host:
            self._connect()

    @property
    def enabled(self) -> bool:
        return bool(self.host) and self._client is not None

    def _connect(self) -> None:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hotspot-dashboard")
            if self.username:
                client.username_pw_set(self.username, self.password)
            client.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)
            client.on_connect = self._on_connect
            client.connect_async(self.host, self.port, keepalive=30)
            client.loop_start()
            self._client = client
        except Exception:
            self._client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # paho 2.x reason_code is a ReasonCode object that compares equal to 0 on success
        if reason_code == 0:
            client.publish(AVAILABILITY_TOPIC, payload="online", retain=True)
            # Re-announce every known hotspot -- covers first connect and
            # any broker reconnect after a drop.
            with self._lock:
                hotspots = list(self._hotspots)
                rig_enabled = self._rig_enabled
            for hs in hotspots:
                self._publish_discovery(hs)
            if rig_enabled:
                self._publish_rig_discovery()

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client:
            try:
                client.publish(AVAILABILITY_TOPIC, payload="offline", retain=True)
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    @staticmethod
    def test_connection(host: str, port: int, username: str, password: str, timeout: float = 5.0):
        """Synchronous connect-and-disconnect for the Settings 'Test' button."""
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hotspot-dashboard-test")
            if username:
                client.username_pw_set(username, password)
            client.connect(host, port or 1883, keepalive=int(timeout))
            client.loop_start()
            deadline = time.time() + timeout
            while time.time() < deadline and not client.is_connected():
                time.sleep(0.1)
            ok = client.is_connected()
            client.loop_stop()
            client.disconnect()
            return (ok, "Connected" if ok else "Could not connect within timeout")
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def set_hotspots(self, hotspots: list) -> None:
        """Update the known hotspot list and (re-)publish discovery configs
        for all of them. Cheap to call whenever the hotspot list changes --
        discovery messages are retained, so this is idempotent."""
        with self._lock:
            self._hotspots = list(hotspots)
        if not self.enabled:
            return
        for hs in hotspots:
            self._publish_discovery(hs)

    def set_rig_enabled(self, enabled: bool) -> None:
        """Turns the Rig Panel's MQTT device on/off -- called from
        app.py's _rebuild_rig_panel() whenever rig_control_enabled or the
        MQTT settings change. Mirrors set_hotspots()'s shape: cheap to
        call every settings save, (re-)publishes discovery immediately
        when turning on."""
        with self._lock:
            was_enabled = self._rig_enabled
            self._rig_enabled = bool(enabled)
        if self._rig_enabled and not was_enabled:
            self._publish_rig_discovery()

    def _publish_rig_discovery(self) -> None:
        if not self.enabled:
            return
        device = {
            "identifiers":  [RIG_NODE_ID],
            "name":         "Station Rig",
            "manufacturer": "W1ZLA Hotspot Dashboard",
            "model":        "Rig Panel (rigctld)",
        }
        for component, object_id, extra in _rig_entity_list():
            unique_id = f"{RIG_NODE_ID}_{object_id}"
            payload = {
                "name":               extra.pop("name"),
                "unique_id":          unique_id,
                "state_topic":        RIG_STATE_TOPIC,
                "availability_topic": AVAILABILITY_TOPIC,
                "device":             device,
                **extra,
            }
            topic = f"{DISCOVERY_PREFIX}/{component}/{RIG_NODE_ID}/{object_id}/config"
            try:
                self._client.publish(topic, json.dumps(payload), retain=True)
            except Exception:
                pass

    def publish_rig_state(self, snap: dict) -> None:
        """snap is rig_panel_poller.snapshot()'s dict -- called from the
        same _mqtt_publish_loop() that publishes per-hotspot state. A
        no-op whenever rig publishing isn't turned on (set_rig_enabled),
        same opt-in gate every other MQTT entity here already has."""
        with self._lock:
            if not self._rig_enabled:
                return
        if not self.enabled:
            return
        freq_hz = snap.get("freq_hz")
        payload = {
            "ptt":      bool(snap.get("ptt")),
            "freq_mhz": round(freq_hz / 1_000_000, 6) if freq_hz else None,
            "mode":     snap.get("mode") or "N/A",
            "band":     snap.get("band") or "N/A",
            "power_w":  snap.get("power_w"),
        }
        try:
            self._client.publish(RIG_STATE_TOPIC, json.dumps(payload))
        except Exception:
            pass

    def _publish_discovery(self, hotspot: dict) -> None:
        if not self.enabled:
            return
        name = hotspot["name"]
        node = _node_id(hotspot["id"])
        state_topic = f"{STATE_PREFIX}/{node}/state"
        device = {
            "identifiers":  [node],
            "name":         name,
            "manufacturer": "W1ZLA Hotspot Dashboard",
            "model":        "MMDVM Hotspot",
        }
        for component, object_id, extra in _entity_list(hotspot):
            unique_id = f"{node}_{object_id}"
            payload = {
                "name":               extra.pop("name"),
                "unique_id":          unique_id,
                "state_topic":        state_topic,
                "availability_topic": AVAILABILITY_TOPIC,
                "device":             device,
                **extra,
            }
            topic = f"{DISCOVERY_PREFIX}/{component}/{node}/{object_id}/config"
            try:
                self._client.publish(topic, json.dumps(payload), retain=True)
            except Exception:
                pass

    def publish_state(self, hotspot_status: dict) -> None:
        """hotspot_status is one entry from monitor.snapshot() (already a dict)."""
        if not self.enabled:
            return
        node = _node_id(hotspot_status["id"])
        payload = {
            "online":          hotspot_status.get("status") == "Online",
            "is_active":       bool(hotspot_status.get("is_active")),
            "favorite_active": bool(hotspot_status.get("is_active") and hotspot_status.get("is_favorite")),
            "mode":            hotspot_status.get("mode") or "N/A",
            "active_call":     hotspot_status.get("active_call") or "None",
            "talkgroup":       hotspot_status.get("talkgroup") or "None",
            "temperature_c":   _extract_float(hotspot_status.get("temperature")),
            "cpu_pct":         _extract_float(hotspot_status.get("cpu")),
            "rssi_dbm":        _extract_float(hotspot_status.get("rssi")),
            "bm_status_text":  hotspot_status.get("bm_status_text") or "N/A",
            "dashboard_update_available": bool(hotspot_status.get("dashboard_update_available")),
            "linked_nodes_count": len(hotspot_status.get("asl_linked_nodes") or []),
            "dmr_static_tgs": ", ".join(
                str(tg.get("talkgroup")) for tg in (hotspot_status.get("bm_static_tgs") or [])
                if tg.get("talkgroup")
            ) or "None",
            "dstar_reflector": hotspot_status.get("ircddb_reflector") or "Not linked",
            "ysf_reflector":   hotspot_status.get("ysf_reflector") or "Not linked",
            "p25_talkgroup":   hotspot_status.get("p25_reflector") or "Not linked",
            "nxdn_talkgroup":  hotspot_status.get("nxdn_reflector") or "Not linked",
        }
        try:
            self._client.publish(f"{STATE_PREFIX}/{node}/state", json.dumps(payload))
        except Exception:
            pass
