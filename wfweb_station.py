"""One-way sync of this app's own station_grid setting into wfweb 0.9.1+'s
new server-side station identity (v5.6) -- confirmed live (2026-09)
against the user's real wfweb: `GET /api/v1/station` returns
`{"callsign":"W1ZLA","grid":"FN43"}`, no auth, plain HTTP, same REST
port wfweb_logbook.py already talks to (NOT the WebSocket power-control
port).

Deliberately GRID-ONLY, one-way (this app -> wfweb), never the other
direction and never the callsign field:
  - This app has no single unified "my callsign" setting to push (every
    existing callsign field -- qrz_quick_log_callsign, pota_callsign,
    rbn_callsign, psk_reporter_callsign, dxcluster_callsign -- is
    deliberately scoped to its own integration, per this project's own
    established "no single global one" convention). Sending an empty or
    wrong callsign could silently clobber the user's real stored
    callsign on wfweb (REST_API.md documents an empty *grid* as
    "keeps the stored one", but says nothing about callsign, so this
    module never risks it -- it simply never sends that field at all).
  - `station_grid` already exists in this app for its own unrelated
    purposes (QTH-line distances, WSPR grid_to_latlon, etc.) -- pushing
    it into wfweb avoids entering the same grid square twice, not a new
    concept this app is introducing.
"""
import json
import urllib.error
import urllib.request

TIMEOUT = 8


def get_station(host: str, port) -> "dict | None":
    """Read-only -- for a Settings display of what wfweb currently has
    stored, no side effects. Returns None on any failure (never raises,
    same degrade-gracefully contract as every other client here)."""
    host = (host or "").strip()
    if not host:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8081
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/v1/station",
            headers={"User-Agent": "hotspot-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return {"callsign": data.get("callsign"), "grid": data.get("grid")}
    except Exception:
        return None


def push_grid(host: str, port, grid: str) -> "tuple[bool, str]":
    """PUTs only {"grid": grid} -- the callsign field is never included,
    see module docstring."""
    host = (host or "").strip()
    grid = (grid or "").strip()
    if not host:
        return False, "No wfweb host configured"
    if not grid:
        return False, "No station grid to sync"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8081
    try:
        body = json.dumps({"grid": grid}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/api/v1/station", data=body, method="PUT",
            headers={"User-Agent": "hotspot-dashboard/1.0", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, "Synced"
    except urllib.error.URLError as e:
        return False, f"Can't reach wfweb at {host}:{port} ({e.reason})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
