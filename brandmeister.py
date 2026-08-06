"""Minimal client for the Brandmeister v2 ("Halligan") REST API.

Confirmed against the live OpenAPI spec at
https://api.brandmeister.network/api-docs?api-docs.json (2026-07) --
the older /v1.0/repeater/?action=profile endpoint documented in various
2019-era blog posts is gone; Brandmeister moved to this v2 API since.

Base URL: https://api.brandmeister.network/v2/
No API key/auth needed for the read-only GETs used here (auth is only
required on the write endpoints -- POST/DELETE talkgroup subscriptions --
which this dashboard doesn't use).

Endpoints, all keyed by <id> = the hotspot's Brandmeister/CCS7 ID:
  GET /device/{id}            -- basic info, including `statusText`
                                  (e.g. "DMO", not a simple online/offline
                                  flag -- shown as-is rather than guessed
                                  at)
  GET /device/{id}/talkgroup  -- array of static talkgroup subscriptions

  POST   /device/{id}/talkgroup             -- add a static TG, body
                                                {"talkgroup": <int>, "slot": <1|2>}
  DELETE /device/{id}/talkgroup/{slot}/{group} -- remove one

The two write endpoints DO need auth -- a per-user API key generated from
the user's own Brandmeister dashboard (Profile Settings -> API Keys),
sent as `Authorization: Bearer <key>`. Confirmed shape directly from the
live OpenAPI spec above (no separate login/token-exchange endpoint --
the generated key IS the bearer token). NOT live-tested against a real
Brandmeister account (none available in this dev environment) -- if
linking/unlinking ever fails in a way that doesn't match the error
messages below, re-verify against a real key/device the same discipline
as every other reverse-engineered integration in this project, don't
assume the request shape is still right.
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

    def invalidate(self, repeater_id: str) -> None:
        """Drop any cached lookup() result for this device -- called after
        a successful write so the very next lookup() (used to reflect the
        change back into monitor.py's live snapshot) hits the network
        instead of returning what's now a stale cached list."""
        with self._lock:
            self._cache.pop(str(repeater_id).strip(), None)

    def _write_request(self, method: str, url: str, api_key: str, body: Optional[dict] = None):
        """Returns (ok: bool, message: str). Never raises -- same
        degrade-gracefully contract as every other client in this app."""
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers={
                "User-Agent":    config.BRANDMEISTER_AGENT,
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            })
            with urllib.request.urlopen(req, timeout=config.BRANDMEISTER_TIMEOUT) as resp:
                resp.read()
            return True, "ok"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            if e.code == 401:
                return False, "Brandmeister rejected the API key (401 Unauthorized)"
            if e.code == 403:
                return False, "API key doesn't have permission for this device (403 Forbidden)"
            return False, f"Brandmeister returned HTTP {e.code}" + (f": {detail}" if detail else "")
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def set_static_talkgroup(self, repeater_id: str, tg: int, slot: int, api_key: str):
        """POST a new static talkgroup subscription. Returns (ok, message)."""
        repeater_id = str(repeater_id).strip()
        ok, message = self._write_request(
            "POST", f"{BM_BASE_URL}device/{repeater_id}/talkgroup", api_key,
            body={"talkgroup": tg, "slot": slot},
        )
        if ok:
            self.invalidate(repeater_id)
            message = f"Linked TG {tg} on TS{slot}"
        return ok, message

    def remove_static_talkgroup(self, repeater_id: str, tg: int, slot: int, api_key: str):
        """DELETE an existing static talkgroup subscription. Returns (ok, message)."""
        repeater_id = str(repeater_id).strip()
        ok, message = self._write_request(
            "DELETE", f"{BM_BASE_URL}device/{repeater_id}/talkgroup/{slot}/{tg}", api_key,
        )
        if ok:
            self.invalidate(repeater_id)
            message = f"Unlinked TG {tg} on TS{slot}"
        return ok, message
