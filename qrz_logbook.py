"""Read-only sync of a QRZ Logbook into qsos.json.

This is a DIFFERENT API from qrz.py's XML callsign lookup:

  * qrz.py            -> xmldata.qrz.com  -- callsign name/address lookup,
                          needs a paid XML subscription.
  * this module       -> logbook.qrz.com/api -- your own logbook's QSO
                          records, authenticated with a per-logbook API
                          KEY (QRZ Logbook -> Settings -> "This logbook is
                          enabled for the QRZ API ... KEY: ..."), NOT the
                          XML credentials. The basic read side works with
                          just the key.

Protocol (application/x-www-form-urlencoded both directions):

  POST body: KEY=<key>&ACTION=STATUS
             KEY=<key>&ACTION=FETCH&OPTION=AFTERLOGID:<n>,MAX:<n>,TYPE:ADIF
             KEY=<key>&ACTION=FETCH&OPTION=LOGIDS:<a,b,c>,TYPE:ADIF
  response:  RESULT=OK&COUNT=<n>&...&ADIF=<url-encoded ADIF text>
             RESULT=FAIL&REASON=...   /   RESULT=AUTH&REASON=...

Each returned ADIF record carries APP_QRZLOG_LOGID -- a monotonic
per-logbook integer, used as BOTH the incremental cursor (OPTION
AFTERLOGID) and the dedupe key in storage.merge_qrz_qsos.

NOT LIVE-VERIFIED from this dev environment -- logbook.qrz.com is outside
the egress allowlist. Two things to confirm against a real logbook
response before trusting them (same "verify against the real thing"
discipline as openspot.py/wsjtx.py):

  1. Which field marks a QSO CONFIRMED. _is_confirmed() below accepts any
     of APP_QRZLOG_STATUS == "C", QSL_RCVD == "Y", or
     APP_QRZLOG_QSL_RCVD == "Y" -- belt and braces until the real one is
     known. STATUS's own CONFIRMED count is used only as a coarse
     "did anything get confirmed since last time" hint.
  2. The exact OPTION separator / whether MAX and TYPE are honored on
     FETCH. If a first real sync returns everything in one giant response
     or errors on the OPTION string, that's where to look.

Confirmation is a STATE CHANGE on an existing record, so the AFTERLOGID
incremental pull can't see it -- _confirm_sweep() re-fetches the specific
LOGIDS of still-unconfirmed, not-too-old local QSOs and diffs. One
Notifications event ("confirmed contact") fires per QSO that actually
flips, never for one pulled in already-confirmed (surface a transition,
not a state -- same rule as monitor.py's fleet events /
hf_conditions.py's solar alerts).
"""
import datetime
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import config
import storage
import storage_notifications
from adif import parse_adif

_NOTIF_SOURCE = "qrz_confirm"


def _post(body: str) -> str:
    req = urllib.request.Request(
        config.QRZ_LOGBOOK_URL,
        data=body.encode("utf-8"),
        headers={
            "User-Agent": config.QRZ_LOGBOOK_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.QRZ_LOGBOOK_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse_response(text: str) -> dict:
    """QRZ's response is &-joined KEY=VALUE pairs, values url-encoded.
    Returns a plain dict with UPPERCASE keys (RESULT, COUNT, ADIF, ...)."""
    out = {}
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip().upper()] = urllib.parse.unquote_plus(v)
    return out


def _is_confirmed(rec: dict) -> bool:
    if (rec.get("APP_QRZLOG_STATUS") or "").strip().upper() == "C":
        return True
    if (rec.get("QSL_RCVD") or "").strip().upper() == "Y":
        return True
    if (rec.get("APP_QRZLOG_QSL_RCVD") or "").strip().upper() == "Y":
        return True
    return False


def _adif_freq_to_hz(freq_mhz_str) -> "int | None":
    if not freq_mhz_str:
        return None
    try:
        return round(float(freq_mhz_str) * 1_000_000)
    except (TypeError, ValueError):
        return None


def _adif_dt_to_epoch(qso_date: str, time_on) -> "float | None":
    """QSO_DATE YYYYMMDD + TIME_ON HHMM/HHMMSS, both UTC (ADIF spec).
    Mirrors app.py's _adif_datetime_to_epoch -- kept local so this module
    stays importable without app.py."""
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


class QrzLogbookClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._enabled = False
        self._api_key = ""
        self._ok = False
        self._last_error: "str | None" = None
        self._last_sync: "dict | None" = None
        self._last_confirm_at = 0.0
        self._prev_confirmed_count: "int | None" = None
        self._events: deque = deque(maxlen=config.QRZ_LOGBOOK_MAX_EVENTS)
        # Reseed the confirmation-notification history from disk so a
        # restart doesn't lose it -- appends oldest-to-newest, same
        # replay contract as hf_conditions.py / brandmeister_lastheard.py.
        for payload in storage_notifications.recent(_NOTIF_SOURCE, config.QRZ_LOGBOOK_MAX_EVENTS):
            self._events.append(payload)

    # --- config / status ---

    def configure(self, enabled: bool, api_key: str) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._api_key = (api_key or "").strip()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and bool(self._api_key)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "api_key_set": bool(self._api_key),
                "ok": self._ok,
                "last_error": self._last_error,
                "last_sync": self._last_sync,
                "events": list(reversed(self._events)),
            }

    def events(self) -> list:
        with self._lock:
            return list(reversed(self._events))

    # --- the background loop calls this ---

    def sync(self, lookup_caller_info=None, station_grid: str = "") -> None:
        """Pull new QSOs, then (on the slower confirm cadence) re-check
        unconfirmed ones. Never raises -- records the failure in status()
        and prints it, same degrade-gracefully contract as every other
        integration client here."""
        with self._lock:
            if not (self._enabled and self._api_key):
                return
            key = self._api_key
        try:
            status = self._fetch_status(key)
            add_records = self._fetch_new(key, lookup_caller_info, station_grid)

            confirm_map, confirm_details = {}, {}
            now = time.time()
            new_confirmed_count = status.get("confirmed")
            confirmed_rose = (
                self._prev_confirmed_count is not None
                and new_confirmed_count is not None
                and new_confirmed_count > self._prev_confirmed_count
            )
            due = (now - self._last_confirm_at) >= config.QRZ_LOGBOOK_CONFIRM_INTERVAL
            if due or confirmed_rose:
                confirm_map, confirm_details = self._confirm_sweep(key)
                self._last_confirm_at = now
            self._prev_confirmed_count = new_confirmed_count

            result = storage.merge_qrz_qsos(add_records, confirm_map)

            for lid in result.get("confirmed_logids", []):
                self._emit_confirmed(confirm_details.get(lid, {"qrz_logid": lid}))

            with self._lock:
                self._ok = True
                self._last_error = None
                self._last_sync = {
                    "at": now,
                    "added": result.get("added", 0),
                    "matched": result.get("matched", 0),
                    "confirmed": result.get("confirmed", 0),
                    "total": status.get("count"),
                    "total_confirmed": new_confirmed_count,
                }
        except Exception as e:  # noqa: BLE001 -- must never escape into the loop
            msg = f"{type(e).__name__}: {e}"
            print(f"[qrz_logbook] sync failed: {msg}", flush=True)
            with self._lock:
                self._ok = False
                self._last_error = msg

    # --- internals ---

    def _fetch_status(self, key: str) -> dict:
        body = urllib.parse.urlencode({"KEY": key, "ACTION": "STATUS"})
        data = _parse_response(_post(body))
        self._raise_for_result(data)

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        return {"count": _int(data.get("COUNT")), "confirmed": _int(data.get("CONFIRMED"))}

    def _fetch_new(self, key: str, lookup_caller_info, station_grid: str) -> list:
        stored = storage.load_qsos()
        cursor = max((q.get("qrz_logid") or 0 for q in stored), default=0)
        grid_to_latlon = _grid_to_latlon()
        default_qth = grid_to_latlon(station_grid) if station_grid else None

        records = []
        for _ in range(config.QRZ_LOGBOOK_MAX_PAGES):
            body = urllib.parse.urlencode({
                "KEY": key, "ACTION": "FETCH",
                "OPTION": f"AFTERLOGID:{cursor},MAX:{config.QRZ_LOGBOOK_FETCH_PAGE},TYPE:ADIF",
            })
            data = _parse_response(_post(body))
            self._raise_for_result(data)
            page = parse_adif(data.get("ADIF", ""))
            if not page:
                break
            page_max = cursor
            for r in page:
                lid = _int_or_none(r.get("APP_QRZLOG_LOGID"))
                if lid is None or lid <= cursor:
                    continue
                page_max = max(page_max, lid)
                built = self._build_qso(r, lid, lookup_caller_info, grid_to_latlon, default_qth)
                if built is not None:
                    records.append(built)
            if page_max <= cursor:
                break
            cursor = page_max
            if len(page) < config.QRZ_LOGBOOK_FETCH_PAGE:
                break
        return records

    def _confirm_sweep(self, key: str):
        """Re-fetch the LOGIDS of stored QRZ QSOs that are still
        unconfirmed and younger than QRZ_LOGBOOK_CONFIRM_MAX_AGE_DAYS,
        and report which are now confirmed."""
        cutoff = time.time() - config.QRZ_LOGBOOK_CONFIRM_MAX_AGE_DAYS * 86400
        pending = []
        for q in storage.load_qsos():
            lid = q.get("qrz_logid")
            if lid is None or q.get("qrz_confirmed"):
                continue
            ts = q.get("logged_at")
            if ts is not None and ts < cutoff:
                continue
            pending.append(int(lid))
        confirm_map, details = {}, {}
        chunk = config.QRZ_LOGBOOK_LOGID_CHUNK
        for i in range(0, len(pending), chunk):
            ids = pending[i:i + chunk]
            body = urllib.parse.urlencode({
                "KEY": key, "ACTION": "FETCH",
                "OPTION": "LOGIDS:" + ",".join(str(x) for x in ids) + ",TYPE:ADIF",
            })
            data = _parse_response(_post(body))
            self._raise_for_result(data)
            for r in parse_adif(data.get("ADIF", "")):
                lid = _int_or_none(r.get("APP_QRZLOG_LOGID"))
                if lid is None or not _is_confirmed(r):
                    continue
                confirm_map[lid] = time.time()
                details[lid] = {
                    "qrz_logid": lid,
                    "call": (r.get("CALL") or "").strip().upper(),
                    "band": (r.get("BAND") or "").strip().lower(),
                    "mode": (r.get("MODE") or "").strip().upper(),
                    "qso_date": (r.get("QSO_DATE") or "").strip(),
                }
        return confirm_map, details

    def _build_qso(self, r: dict, lid: int, lookup_caller_info, grid_to_latlon, default_qth) -> "dict | None":
        call = (r.get("CALL") or "").strip().upper()
        if not call:
            return None
        grid = (r.get("GRIDSQUARE") or "").strip()
        latlon = grid_to_latlon(grid) if grid else None

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
        qth = grid_to_latlon(my_grid) if my_grid else None
        if qth is None:
            qth = default_qth
        qth_lat, qth_lon = qth if qth is not None else (None, None)

        qso_date = (r.get("QSO_DATE") or "").strip()
        confirmed = _is_confirmed(r)
        return {
            "call": call,
            "band": (r.get("BAND") or "").strip().lower(),
            "mode": (r.get("MODE") or "").strip().upper(),
            "date": qso_date,
            "grid": grid or None,
            "frequency_hz": _adif_freq_to_hz(r.get("FREQ")),
            "lat": lat, "lon": lon,
            "qth_lat": qth_lat, "qth_lon": qth_lon,
            "name": name, "location": location,
            "city": city, "state": state, "country": country,
            "rst_sent": (r.get("RST_SENT") or "").strip() or None,
            "rst_rcvd": (r.get("RST_RCVD") or "").strip() or None,
            "logged_at": _adif_dt_to_epoch(qso_date, r.get("TIME_ON")),
            "source": "qrz",
            "qrz_logid": lid,
            "qrz_confirmed": confirmed,
            "qrz_confirmed_at": time.time() if confirmed else None,
        }

    def _emit_confirmed(self, detail: dict) -> None:
        event = {
            "call": detail.get("call") or "",
            "band": detail.get("band") or "",
            "mode": detail.get("mode") or "",
            "qrz_logid": detail.get("qrz_logid"),
            "at": time.time(),
        }
        with self._lock:
            self._events.append(event)
        storage_notifications.log_notification(
            _NOTIF_SOURCE, event["at"], event, config.QRZ_LOGBOOK_MAX_EVENTS
        )

    @staticmethod
    def _raise_for_result(data: dict) -> None:
        result = (data.get("RESULT") or "").strip().upper()
        if result == "OK":
            return
        reason = data.get("REASON") or data.get("EXTENDED") or "no reason given"
        if result == "AUTH":
            raise RuntimeError(f"QRZ Logbook rejected the API key ({reason})")
        # QRZ returns RESULT=FAIL with REASON "no records" for an empty
        # FETCH -- treat that as a clean empty result, not an error.
        if result == "FAIL" and "record" in reason.lower():
            data["ADIF"] = data.get("ADIF", "")
            return
        raise RuntimeError(f"QRZ Logbook error: {result or 'no RESULT'} -- {reason}")


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _grid_to_latlon():
    """Lazy import -- wspr_activity.py pulls in more than this module
    needs at import time, and mirrors how wsjtx.py imports it locally."""
    from wspr_activity import grid_to_latlon
    return grid_to_latlon
