"""Minimal client for QRZ's XML lookup API.

Spec: https://www.qrz.com/docs/xml/current_spec.html

Two HTTP calls are involved:
  1. Login once -> get a session key, cache and reuse it.
  2. Per callsign -> https://xmldata.qrz.com/xml/current/?s=<key>;callsign=<call>

Looking up *other* people's name/address requires an active QRZ XML
subscription. Without one, lookups come back with a "subscription required"
message -- this client returns None in that case so the dashboard degrades
gracefully.
"""
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

import config

QRZ_BASE_URL = "https://xmldata.qrz.com/xml/current/"


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(elem: Optional[ET.Element], tag: str) -> Optional[ET.Element]:
    if elem is None:
        return None
    for node in elem:
        if _local_tag(node.tag) == tag:
            return node
    return None


def _text(elem: Optional[ET.Element], tag: str) -> Optional[str]:
    node = _child(elem, tag)
    return node.text.strip() if node is not None and node.text else None


def _float(elem: Optional[ET.Element], tag: str) -> Optional[float]:
    raw = _text(elem, tag)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class QrzClient:
    def __init__(self, username: str, password: str, agent: str = "hotspot-dashboard/1.0"):
        self.username = username
        self.password = password
        self.agent    = agent
        self._session_key: Optional[str] = None
        self._lock  = threading.Lock()
        self._cache: dict = {}  # callsign -> (expiry_epoch, result|None)

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def lookup(self, callsign: str) -> Optional[dict]:
        """Return dict with name/location/image_url/lat/lon or None."""
        if not self.enabled:
            return None
        cached = self._cache.get(callsign)
        if cached and cached[0] > time.time():
            return cached[1]
        result = self._lookup_remote(callsign)
        self._cache[callsign] = (time.time() + config.QRZ_CACHE_TTL, result)
        return result

    def cached_lookup(self, callsign: str) -> Optional[dict]:
        """Return the cached result without making a new network request.
        Used by the map endpoint to annotate history entries without
        triggering extra QRZ calls for stale callsigns."""
        cached = self._cache.get(callsign)
        if cached and cached[0] > time.time():
            return cached[1]
        return None

    # --- internals ---

    def _lookup_remote(self, callsign: str) -> Optional[dict]:
        try:
            key = self._get_session_key()
            if not key:
                return None
            root = self._request({"s": key, "callsign": callsign})
            session = _child(root, "Session")
            if session is not None and _text(session, "Error"):
                with self._lock:
                    self._session_key = None
                return None
            cs = _child(root, "Callsign")
            if cs is None:
                return None
            return {
                "name":      self._compose_name(cs),
                "location":  self._compose_location(cs),
                # city/state/country kept SEPARATE too (not just parsed back
                # out of "location", which is lossy -- it drops country
                # entirely whenever a state is present). Added for the
                # Recent Contacts card, which wants a country flag and
                # city/state as distinct fields, not one combined string.
                # "location" itself is untouched -- every existing caller
                # (hotspot card caller display, etc.) keeps working as-is.
                "city":      _text(cs, "addr2"),
                "state":     _text(cs, "state"),
                "country":   _text(cs, "country"),
                "image_url": _text(cs, "image"),
                "lat":       _float(cs, "lat"),
                "lon":       _float(cs, "lon"),
            }
        except Exception:
            return None

    @staticmethod
    def _compose_name(cs: ET.Element) -> Optional[str]:
        fname = _text(cs, "fname") or ""
        lname = _text(cs, "name")  or ""
        full  = " ".join(p for p in (fname, lname) if p)
        return full or None

    @staticmethod
    def _compose_location(cs: ET.Element) -> Optional[str]:
        city    = _text(cs, "addr2")
        state   = _text(cs, "state")
        country = _text(cs, "country")
        parts   = [p for p in (city, state or country) if p]
        return ", ".join(parts) if parts else None

    def _get_session_key(self) -> Optional[str]:
        with self._lock:
            if self._session_key:
                return self._session_key
            root    = self._request({"username": self.username, "password": self.password})
            session = _child(root, "Session")
            if session is None or _text(session, "Error"):
                return None
            self._session_key = _text(session, "Key")
            return self._session_key

    def _request(self, params: dict) -> ET.Element:
        params = {**params, "agent": self.agent}
        url    = QRZ_BASE_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=config.QRZ_TIMEOUT) as resp:
            data = resp.read()
        return ET.fromstring(data)
