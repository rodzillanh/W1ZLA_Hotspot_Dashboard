"""Minimal client for AllStarLink's public stats API, used to resolve an
ASL3 node's currently-linked nodes to callsign/description/location.

API (undocumented but stable, confirmed against a real query 2026-07):
  https://stats.allstarlink.org/api/stats/<node>
No API key or login required. Querying a node returns that node's own
"linkedNodes" array, each entry optionally carrying a resolved "callsign",
"node_frequency" (frequency/description text) and "server.Location" -- so
one HTTP call resolves every node currently linked to it, rather than
needing a separate lookup per linked node. Not every linked node has this
data on file (private/unregistered nodes just show their bare number);
callers should fall back to the node number in that case.
"""
import time
import threading
import urllib.request
import urllib.error
import json

import config

ASLSTATS_BASE_URL = "https://stats.allstarlink.org/api/stats/"


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AslStatsClient:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cache: dict = {}  # node -> (expiry_epoch, {linked_node: {callsign, description, location}})
        self._rate_lock = threading.Lock()  # separate from _lock -- guards _last_live_fetch_at only, never held while sleeping
        self._last_live_fetch_at = 0.0

    def _throttle_live_request(self):
        """Self-rate-limits this client's own outbound requests to
        stats.allstarlink.org (confirmed live: 30 req/min per source IP,
        shared across every caller on this box). Without this, a burst of
        several favorites' caches expiring at once (they were all
        populated in the same original burst, so they expire together
        too) fired one request per node with zero spacing -- confirmed
        live to trip a 429 on every request in that burst simultaneously.
        The wait-time is computed under the lock but slept OUTSIDE it, so
        this never blocks other threads' cache-hit lookups."""
        with self._rate_lock:
            now = time.time()
            wait = self._last_live_fetch_at + config.ASLSTATS_MIN_LIVE_INTERVAL_SEC - now
            self._last_live_fetch_at = (now + wait) if wait > 0 else now
        if wait > 0:
            time.sleep(wait)

    def linked_node_info(self, node: str) -> dict:
        """Return {linked_node_number: {"callsign": str|None, "description":
        str|None, "location": str|None}} for the given node's current links.
        Empty dict on any failure; a link with no data on file simply isn't
        a key in the result -- callers fall back to showing the bare node
        number in that case."""
        with self._lock:
            cached = self._cache.get(node)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._lookup_remote(node)
        with self._lock:
            self._cache[node] = (time.time() + config.ASLSTATS_CACHE_TTL, result)
        return result

    def favorite_stats(self, node: str) -> dict:
        """Per-node live stats for the ASL Control sidebar's Favorites
        list -- Active/registered status, Web-Transceiver flag, Rx%
        (totaltxtime/apprptuptime, matching AllScan's own documented
        formula), and LCnt (that node's own current link count). Cached
        separately from linked_node_info() (own cache key prefix) since
        this reads the TOP-LEVEL node/stats.data of the queried node
        itself, not the linkedNodes sub-array the other method extracts.

        Deliberately does NOT return a "keyed" field -- confirmed live
        via AllScan's own changelog that the stats API's keyed value is
        unreliable for many nodes ("shows a 0 value even when the node
        is in fact keyed"), and AllScan's own fix requires polling twice
        and diffing totalkeyups/totaltxtime. This app has a genuinely
        reliable keyed signal already for anything actually linked to a
        controlling hotspot (asl_linked_nodes, SSH-sourced) -- callers
        should use that instead of trusting a "maybe keyed" guess from
        here for favorites that aren't currently connected."""
        cache_key = f"fav:{node}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > time.time():
                return cached[1]
        result = self._fetch_favorite_stats(node)
        with self._lock:
            self._cache[cache_key] = (time.time() + config.ASLSTATS_FAVORITE_CACHE_TTL, result)
        return result

    def _fetch_favorite_stats(self, node: str) -> dict:
        self._throttle_live_request()
        try:
            req = urllib.request.Request(
                f"{ASLSTATS_BASE_URL}{node}",
                headers={"User-Agent": config.ASLSTATS_AGENT},
            )
            with urllib.request.urlopen(req, timeout=config.ASLSTATS_TIMEOUT) as resp:
                data = json.loads(resp.read())
            node_info  = data.get("node") or {}
            stats_data = (data.get("stats") or {}).get("data") or {}
            # apprptuptime/totaltxtime arrive as STRINGS in the real API
            # response (confirmed live: "apprptuptime":"73285", quoted) --
            # NOT numbers, despite looking indistinguishable from one
            # through a bare print() during earlier research, which is
            # exactly how this shipped broken the first time: dividing
            # them directly raised a silent TypeError on every node that
            # actually WAS found, caught by the generic except below and
            # misreported as "not found" -- only genuine 404s (which never
            # reach this line) displayed correctly, making the bug look
            # like the opposite of what it was.
            uptime = _to_int(stats_data.get("apprptuptime"))
            txtime = _to_int(stats_data.get("totaltxtime"))
            rx_pct = round(txtime / uptime * 100, 1) if uptime else None
            links  = stats_data.get("links")
            return {
                "found":          True,
                "status":         node_info.get("Status"),
                "webtransceiver": node_info.get("access_webtransceiver") == "1",
                "rx_pct":         rx_pct,
                "lcnt":           len(links) if isinstance(links, list) else None,
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"found": False}  # confirmed live: a real 404 for an unregistered/bogus node number
            # A non-404 HTTP error (rate limit, server hiccup) is NOT the
            # same claim as "this node doesn't exist" -- found=None (vs.
            # False) lets callers show "unknown/unavailable" instead of a
            # confident, potentially wrong "Not in ASL DB".
            print(f"aslstats: favorite_stats({node}) HTTP {e.code} from stats.allstarlink.org"
                  + (" -- likely rate-limited (30 req/min)" if e.code == 429 else ""))
            return {"found": None, "error": True}
        except Exception as e:
            # Printed (not raised) to keep this integration's silent-degrade
            # contract intact -- but printed, not swallowed outright, since
            # "every favorite shows Stats unavailable" is otherwise
            # undiagnosable from the outside (confirmed live: this exact
            # symptom shipped once with zero trace anywhere to explain it).
            print(f"aslstats: favorite_stats({node}) failed: {type(e).__name__}: {e}")
            return {"found": None, "error": True}

    def _lookup_remote(self, node: str) -> dict:
        self._throttle_live_request()
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
                name = str(entry.get("name", "")).strip()
                if not name:
                    continue
                callsign    = entry.get("callsign")
                description = entry.get("node_frequency") or None
                location    = ((entry.get("server") or {}).get("Location")) or None
                if callsign or description or location:
                    linked[name] = {
                        "callsign":    callsign,
                        "description": description,
                        "location":    location,
                    }
            return linked
        except Exception as e:
            print(f"aslstats: linked_node_info({node}) failed: {type(e).__name__}: {e}")
            return {}
