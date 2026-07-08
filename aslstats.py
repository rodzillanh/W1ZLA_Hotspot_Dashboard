"""Minimal client for AllStarLink's public stats API, used to resolve an
ASL3 node's currently-linked nodes to callsigns.

API (undocumented but stable, confirmed against a real query 2026-07):
  https://stats.allstarlink.org/api/stats/<node>
No API key or login required. Querying a node returns that node's own
"linkedNodes" array, each entry optionally carrying a resolved "callsign" --
so one HTTP call resolves every node currently linked to it, rather than
needing a separate lookup per linked node. Not every linked node has a
callsign on file (private/unregistered nodes just show their bare number);
callers should fall back to the node number in that case.
"""
import time
import threading
import urllib.request
import json

import config

ASLSTATS_BASE_URL = "https://stats.allstarlink.org/api/stats/"


class AslStatsClient:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cache: dict = {}  # node -> (expiry_epoch, {linked_node: callsign})

    def linked_callsigns(self, node: str) -> dict:
        """Return {linked_node_number: callsign} for the given node's
        current links. Empty dict on any failure or if a link has no
        callsign on file -- callers fall back to showing the bare number."""
        with self._lock:
            cached = self._cache.get(node)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._lookup_remote(node)
        with self._lock:
            self._cache[node] = (time.time() + config.ASLSTATS_CACHE_TTL, result)
        return result

    def _lookup_remote(self, node: str) -> dict:
        try:
            req = urllib.request.Request(
                f"{ASLSTATS_BASE_URL}{node}",
                headers={"User-Agent": config.ASLSTATS_AGENT},
            )
            with urllib.request.urlopen(req, timeout=config.ASLSTATS_TIMEOUT) as resp:
                data = json.loads(resp.read())
            linked_nodes = (data.get("stats") or {}).get("data", {}).get("linkedNodes") or []
            linked = {}
            for entry in linked_nodes:
                name     = str(entry.get("name", "")).strip()
                callsign = entry.get("callsign")
                if name and callsign:
                    linked[name] = callsign
            return linked
        except Exception:
            return {}
