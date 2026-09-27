"""Sync of wfweb's own server-side ADIF logbook (v0.9.1+) into qsos.json,
forwarding genuinely new QSOs to QRZ Logbook on success.

A DIFFERENT integration from qrz_logbook.py (which talks to QRZ) and from
wfweb_power.py (which talks to wfweb's WebSocket, for power/LAN control).
This talks to wfweb's plain-HTTP REST API, on wfweb's OWN separate "REST/
API" port (confirmed live 2026-09: a genuinely different port from the
web UI / WebSocket port `wfweb_host`/`wfweb_port` already cover -- e.g.
8281 vs. 8282 on the dev deployment this was built against, purely a
consequence of how THAT Docker template maps the two container ports
8081/8080; don't assume a fixed relationship between the two on another
install). No auth -- confirmed live (`Access-Control-Allow-Origin: *`,
no token/key anywhere in the request), same trusted-LAN posture every
other integration in this app takes.

Confirmed live 2026-09 against a real wfweb 0.9.1 + IC-7300MK2:

    GET  /api/v1/logbook/export
         -> {"count": n, "ids": [...], "adif": "<ADIF document>"}
         The "download new QSOs" document plus the ids it contains, so a
         client can confirm exactly what it saved before telling wfweb
         to stop offering them again. Confirmed live with an empty
         logbook: {"adif":"ADIF Export from wfweb\\n<ADIF_VER:5>3.1.4
         <PROGRAMID:5>wfweb <EOH>\\n","count":0,"ids":[]} -- a clean,
         well-formed empty response, not an error.
    POST /api/v1/logbook/exported   body {"ids": [...]}
         Marks those QSOs as exported so /export won't return them again.

The exported ADIF deliberately OMITS wfweb's own APP_WFWEB_ID field
(confirmed from wfweb's own source, webserver.cpp: `toAdif(true, false)`
-- the second arg is `appFields`, and both /export and /adif?new=1 pass
`false` -- "a file for other services"). So there is no way to correlate
one returned `id` to one specific ADIF record, and this module doesn't
try to -- dedupe against wfweb re-sending the same record on a LATER
sync is handled entirely by wfweb's own export/exported bookkeeping
(mark everything in a batch exported once it's been safely imported);
dedupe/enrichment against a contact already known from a DIFFERENT
source (WSJT-X live logging, ADIF import, QRZ Logbook sync) is handled
by storage.merge_wfweb_qsos() the same fuzzy-match way qrz_logbook.py's
own merge_qrz_qsos() already does.

Settings -> Integrations -> "wfweb Logbook sync" -- reuses the existing
`wfweb_host` setting (Integrations already has it for wfweb power/LAN
control) plus its own `wfweb_logbook_rest_port`.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import config
import storage
from adif import parse_adif


def _get_json(host: str, port: int, path: str) -> dict:
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "hotspot-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=config.WFWEB_LOGBOOK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _post_json(host: str, port: int, path: str, body: dict) -> dict:
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": "hotspot-dashboard/1.0", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=config.WFWEB_LOGBOOK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _adif_freq_to_hz(freq_mhz_str) -> "int | None":
    if not freq_mhz_str:
        return None
    try:
        return round(float(freq_mhz_str) * 1_000_000)
    except (TypeError, ValueError):
        return None


def _adif_dt_to_epoch(qso_date: str, time_on) -> "float | None":
    """Mirrors qrz_logbook.py's own _adif_dt_to_epoch -- kept local so
    this module stays independently importable, same reasoning that
    module's own docstring gives for not sharing app.py's copy."""
    import datetime
    if not qso_date or len(qso_date) != 8:
        return None
    t = (time_on or "").strip()
    if len(t) == 4:
        t += "00"
    elif len(t) != 6:
        return None
    try:
        return datetime.datetime(
            int(qso_date[0:4]), int(qso_date[4:6]), int(qso_date[6:8]),
            int(t[0:2]), int(t[2:4]), int(t[4:6]),
            tzinfo=datetime.timezone.utc,
        ).timestamp()
    except ValueError:
        return None


class WfwebLogbookClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._enabled = False
        self._host = ""
        self._port = 8081
        self._ok = False
        self._last_error: "str | None" = None
        self._last_sync: "dict | None" = None

    def configure(self, enabled: bool, host: str, port) -> None:
        try:
            p = int(port)
        except (TypeError, ValueError):
            p = 8081
        with self._lock:
            self._enabled = bool(enabled)
            self._host = (host or "").strip()
            self._port = p

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and bool(self._host)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "host_set": bool(self._host),
                "ok": self._ok,
                "last_error": self._last_error,
                "last_sync": self._last_sync,
            }

    def test_connection(self, host: str, port) -> "tuple[bool, str]":
        h = (host or "").strip()
        if not h:
            return False, "No wfweb host configured (Settings -> Integrations)"
        try:
            p = int(port)
        except (TypeError, ValueError):
            p = 8081
        try:
            data = _get_json(h, p, "/api/v1/logbook")
            total = data.get("total", 0)
            return True, f"wfweb logbook reachable -- {total} QSO{'s' if total != 1 else ''} on record"
        except urllib.error.URLError as e:
            return False, f"Can't reach wfweb at {h}:{p} ({e.reason})"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def sync(self, qrz_client=None, lookup_caller_info=None, station_grid: str = "") -> None:
        """Pull unexported QSOs from wfweb, import into qsos.json (dedupe/
        enrich against anything already known from another source), push
        genuinely new ones to QRZ if `qrz_client` is configured, then tell
        wfweb they're exported so it won't offer them again. Never raises
        -- records the failure in status() and prints it, same
        degrade-gracefully contract as every other integration client."""
        with self._lock:
            if not (self._enabled and self._host):
                return
            host, port = self._host, self._port
        if not self._sync_lock.acquire(blocking=False):
            return  # a sync is already in progress
        try:
            exp = _get_json(host, port, "/api/v1/logbook/export")
            ids = exp.get("ids") or []
            adif_text = exp.get("adif") or ""
            records = parse_adif(adif_text) if ids else []

            grid_to_latlon = _grid_to_latlon()
            default_qth = grid_to_latlon(station_grid) if station_grid else None
            stored = storage.load_qsos()

            built = []
            for r in records:
                call = (r.get("CALL") or "").strip().upper()
                if not call:
                    continue
                band = (r.get("BAND") or "").strip().lower()
                mode = (r.get("MODE") or "").strip().upper()
                grid = (r.get("GRIDSQUARE") or "").strip()
                latlon = grid_to_latlon(grid) if grid else None
                qso_date = (r.get("QSO_DATE") or "").strip()
                logged_at = _adif_dt_to_epoch(qso_date, r.get("TIME_ON"))

                name = (r.get("NAME") or "").strip() or None
                city = state = country = location = None
                lat = lon = None
                if lookup_caller_info is not None:
                    try:
                        info = lookup_caller_info(call)
                        name = name or info.get("name")
                        city, state = info.get("city"), info.get("state")
                        country = info.get("country")
                        location = info.get("location")
                        lat, lon = info.get("lat"), info.get("lon")
                    except Exception:
                        pass
                if latlon is not None:
                    lat, lon = latlon
                country = (r.get("COUNTRY") or "").strip() or country

                my_grid = (r.get("MY_GRIDSQUARE") or "").strip()
                qth = grid_to_latlon(my_grid) if my_grid else default_qth
                qth_lat, qth_lon = qth if qth is not None else (None, None)

                candidate = {"call": call, "band": band, "mode": mode, "logged_at": logged_at,
                             "date": qso_date}
                is_new = not any(
                    q.get("source") != "wfweb"
                    and storage.qso_matches(candidate, q, config.WFWEB_LOGBOOK_MATCH_WINDOW_SEC)
                    for q in stored
                )

                qso = {
                    "call": call, "band": band, "mode": mode, "date": qso_date,
                    "grid": grid or None,
                    "frequency_hz": _adif_freq_to_hz(r.get("FREQ")),
                    "lat": lat, "lon": lon,
                    "qth_lat": qth_lat, "qth_lon": qth_lon,
                    "name": name, "location": location,
                    "city": city, "state": state, "country": country,
                    "rst_sent": (r.get("RST_SENT") or "").strip() or None,
                    "rst_rcvd": (r.get("RST_RCVD") or "").strip() or None,
                    "comment": (r.get("COMMENT") or "").strip() or None,
                    "logged_at": logged_at,
                    "source": "wfweb",
                    "qrz_logid": None,
                }

                if is_new and qrz_client is not None:
                    try:
                        result = qrz_client.insert_qso({
                            "CALL": call, "QSO_DATE": qso_date,
                            "TIME_ON": (r.get("TIME_ON") or "").strip(),
                            "BAND": band, "MODE": mode,
                            "RST_SENT": qso["rst_sent"], "RST_RCVD": qso["rst_rcvd"],
                            "FREQ": r.get("FREQ"), "COMMENT": qso["comment"],
                        })
                        qso["qrz_logid"] = result.get("logid")
                    except Exception as e:  # noqa: BLE001 -- QRZ push is best-effort
                        print(f"[wfweb_logbook] QRZ push failed for {call}: "
                              f"{type(e).__name__}: {e}", flush=True)
                built.append(qso)

            result = storage.merge_wfweb_qsos(built)

            if ids:
                _post_json(host, port, "/api/v1/logbook/exported", {"ids": ids})

            with self._lock:
                self._ok = True
                self._last_error = None
                self._last_sync = {
                    "at": time.time(),
                    "pulled": len(ids),
                    "added": result.get("added", 0),
                    "matched": result.get("matched", 0),
                }
        except Exception as e:  # noqa: BLE001 -- must never escape into the loop
            msg = f"{type(e).__name__}: {e}"
            print(f"[wfweb_logbook] sync failed: {msg}", flush=True)
            with self._lock:
                self._ok = False
                self._last_error = msg
        finally:
            self._sync_lock.release()


def _grid_to_latlon():
    """Lazy import -- mirrors qrz_logbook.py's own reasoning for not
    pulling in wspr_activity.py's extra weight at module import time."""
    from wspr_activity import grid_to_latlon
    return grid_to_latlon
