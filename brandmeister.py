"""Minimal client for the Brandmeister v2 ("Halligan") REST API.

Confirmed against the live OpenAPI spec at
https://api.brandmeister.network/api-docs?api-docs.json (2026-07) --
the older /v1.0/repeater/?action=profile endpoint documented in various
2019-era blog posts is gone; Brandmeister moved to this v2 API since.

Base URL: https://api.brandmeister.network/v2/
No API key/auth needed for the read-only GETs used here (auth is only
required on the write endpoints -- POST/DELETE talkgroup subscriptions --
which this dashboard doesn't use).

Two endpoints, both keyed by <id> = the hotspot's Brandmeister/CCS7 ID:
  GET /device/{id}            -- basic info, including `statusText`
                                  (e.g. "DMO", not a simple online/offline
                                  flag -- shown as-is rather than guessed
                                  at)
  GET /device/{id}/talkgroup  -- array of static talkgroup subscriptions
"""
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
import json
from typing import Optional

import config

BM_BASE_URL = "https://api.brandmeister.network/v2/"


class BrandmeisterClient:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cache: dict = {}  # repeater_id -> (expiry_epoch, result|None)

    def lookup(self, repeater_id: str) -> Optional[dict]:
        """Return dict with status_text/static_talkgroups, or None on error/not found."""
        repeater_id = str(repeater_id).strip()
        if not repeater_id:
            return None
        with self._lock:
            cached = self._cache.get(repeater_id)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._lookup_remote(repeater_id)
        with self._lock:
            self._cache[repeater_id] = (time.time() + config.BRANDMEISTER_CACHE_TTL, result)
        return result

    def _lookup_remote(self, repeater_id: str) -> Optional[dict]:
        result, _, _ = self._request(repeater_id)
        return result

    def debug_lookup(self, repeater_id: str) -> dict:
        """Same request as lookup(), but never swallows the failure reason --
        used by the Settings 'Test' button."""
        result, status, detail = self._request(repeater_id)
        return {"result": result, "status": status, "detail": detail}

    def _get_json(self, url: str):
        """Returns (parsed_json_or_None, http_status_or_None, error_detail_or_None)."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": config.BRANDMEISTER_AGENT})
            with urllib.request.urlopen(req, timeout=config.BRANDMEISTER_TIMEOUT) as resp:
                status = resp.status
                raw    = resp.read()
        except urllib.error.HTTPError as e:
            return None, e.code, f"HTTP {e.code} from {url}"
        except Exception as e:
            return None, None, f"{type(e).__name__}: {e}"
        try:
            return json.loads(raw), status, None
        except Exception:
            snippet = raw[:200].decode("utf-8", errors="replace")
            return None, status, f"Response wasn't JSON (HTTP {status}): {snippet!r}"

    def _request(self, repeater_id: str):
        """Returns (parsed_result_or_None, http_status_or_None, detail_str)."""
        device, status, err = self._get_json(f"{BM_BASE_URL}device/{repeater_id}")
        if err:
            return None, status, err
        if not isinstance(device, dict) or not device:
            return None, status, f"Unexpected/empty response for device {repeater_id!r} (HTTP {status})"

        # Static talkgroups are a separate call -- best-effort, since a
        # hotspot with none configured (relying on dynamic/last-heard TG
        # selection) is a normal, valid state, not an error.
        tgs, _, tg_err = self._get_json(f"{BM_BASE_URL}device/{repeater_id}/talkgroup")
        static_tgs = []
        if isinstance(tgs, list):
            static_tgs = [
                {"talkgroup": t.get("talkgroup"), "slot": t.get("slot")}
                for t in tgs if isinstance(t, dict) and t.get("talkgroup")
            ]

        result = {
            "status_text":       device.get("statusText"),
            "callsign":          device.get("callsign"),
            "last_seen":         device.get("last_seen"),
            "static_talkgroups": static_tgs,
        }
        return result, status, "ok"
