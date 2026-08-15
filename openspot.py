"""WebSocket client for openSPOT 4 (SharkRF) hotspots.

Unlike WPSD/ASL3, openSPOT4 has no SSH access at all -- it's a closed
embedded device controlled entirely over HTTP + WebSocket. This whole
protocol was verified live against a real openSPOT 4 Pro (browser
dev-tools network capture), NOT from SharkRF's own published docs
(github.com/sharkrf/osp-http-api etc.), which describe an OLDER openSPOT
generation with different, incompatible endpoint names (e.g.
`checkauth.cgi` there vs. `/checktok` on this firmware -- confirmed by a
live 404 against the documented name, then finding the real one via
browser network capture). Same "verify against the real thing"
discipline as everywhere else in this project.

Confirmed live:
- Auth check: GET /checktok with `Authorization: Bearer <jwt>` -> 200.
- gettok/login (obtaining a fresh JWT) -- initially ported from
  SharkRF's older-gen docs as an unverified guess (the first capture
  session only ever saw an already-issued JWT), but a later fresh-page-
  load capture caught both by name, confirming the guessed paths exactly.
- Live status/call data streams over a WebSocket, not polling:
  ws://<ip>/<jwt> (the JWT goes in the URL path, not a header -- a
  workaround since browser JS can't set custom WS handshake headers),
  Sec-WebSocket-Protocol: openspot4. Confirmed via a captured 101
  Switching Protocols upgrade.
- Real JSON message shapes observed streaming over that socket (see
  _handle_message below for exact per-type handling).

Call lifecycle tracking is driven by "calllog" messages, not the
human-readable "log" text lines -- confirmed live against BOTH a DMR
call (Homebrew/BrandMeister connector) and a C4FM/YSF call (YSF
Reflector connector, "TGIF"), which turned out to have genuinely
different "log" line formats (DMR's has a "[N]" channel bracket; C4FM's
does not, and never emits an equivalent "call started" log line at
all -- only "calllog" reliably signals a call starting for every mode
tested so far). A "calllog" entry with duration 0.0 is a call starting;
the SAME "id" reappearing later with a real (>0) duration is that call
ending, with its own ber/loss/rssi -- this is the same shape for DMR and
C4FM, unlike the "log" lines which differ per mode. The "log" lines are
only still used to opportunistically learn the human mode name (DMR/
YSF) via each mode's own "<x>ct:" prefix -- see _MODE_BY_PREFIX. D-STAR/
NXDN/P25 have not been tested at all; their calllog "src" field shape
(DMR-ID-like vs. already-a-callsign, see _apply_call_start) and mode
prefix are both unverified guesses if they ever come up.

An openSPOT4's admin password is NOT one fixed device-wide credential --
each config profile can have its own separate password (confirmed live:
a working password started returning 401 immediately after switching
the device to a different profile, which reboots it into that profile).
See _describe_login_error() -- a 401 specifically calls this out rather
than a bare "Unauthorized", since it's the most likely cause after a
profile change, not a connection/network problem.
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import websocket

import config
from storage import load_hotspots

# --- HTTP auth endpoints ---

# Ported from SharkRF's older-gen openSPOT docs (github.com/sharkrf/
# osp-http-api) as a starting-point guess -- confirmed correct by a later
# live dev-tools capture of a real fresh page load, which caught both
# gettok and login by name (see module docstring). /checktok was
# confirmed live from the start (validating an *already-issued* JWT).
_GETTOK_PATH = "/gettok"
_LOGIN_PATH = "/login"
_CHECKTOK_PATH = "/checktok"

# Confirmed live via a captured 101 Switching Protocols WS upgrade.
_WS_SUBPROTOCOL = "openspot4"

# Matches just the mode-name prefix off the FRONT of any "log" line that
# has one -- e.g. "dmrct: [0] grp voice call started, ..." or
# "c4fmct: call ended, ...". Deliberately not trying to parse the rest of
# the line (dst/src/id/dur/ber/loss/rssi) -- calllog gives all of that in
# a consistent structured shape for every mode tested, this regex exists
# purely to opportunistically learn the human mode name for display.
_MODE_LOG_RE = re.compile(r"^(\w+ct):")

# Only "dmrct" (DMR) and "c4fmct" (C4FM/YSF) confirmed live. Other modes
# (D-STAR, NXDN, P25) presumably have their own "<x>ct:" prefix --
# unverified guess at the pattern, so anything not in this map falls back
# to stripping "ct" and upper-casing the prefix rather than silently
# dropping the mode entirely.
_MODE_BY_PREFIX = {
    "dmrct": "DMR",
    "c4fmct": "YSF",
}

# openSPOT4's own APRS-IS connection state -- confirmed live against a
# real connect cycle (see _on_aprsbgstate). Only these three values seen.
_APRS_BGSTATE_LABELS = {
    -1: "disconnected",
    0: "connecting",
    2: "connected",
}

# Battery status -- confirmed live from real captured "log" messages on a
# battery-powered openSPOT4, e.g.:
#   "pwr: batt 52% est. 1h41m 3886mv usb 1500ma cpu 43.2°C chg 300ma"
#   "pwr: batt 100% 4149mv usb 1500ma cpu 30.0°C chg 0ma"
# Not in the documented HTTP API at all (checked info.cgi/status.cgi/ip.cgi
# -- no battery field anywhere); this line only ever appears in the same
# live "log" WebSocket message _MODE_LOG_RE already opportunistically reads
# a mode name from, so it's parsed alongside that in _on_log rather than as
# a separate message type. The ENTIRE "est. XhXXm" clause is optional, not
# just its h/m halves -- confirmed by the second real sample above, captured
# at 100% with no discharge estimate at all (nothing to estimate towards
# when not discharging). An earlier version of this regex required "est."
# literally, which meant that specific, common, healthy-battery state
# matched NOTHING and silently dropped battery reporting entirely -- a real
# regression caught from this second sample, not a hypothetical edge case.
# The temp field's trailing unit is matched as "\S*C" rather than a literal
# "°C" -- one captured log file had the degree sign mangled to "Â°C" by
# whatever process saved it to a text file (a classic UTF-8-read-as-
# Latin-1 artifact), so matching loosely up to the next literal "C" is
# robust to that without caring what's actually in between.
_BATTERY_LOG_RE = re.compile(
    r"pwr:\s*batt\s+(?P<pct>\d+)%\s+"
    r"(?:(?P<est>est\.\s+(?:(?P<est_h>\d+)h)?(?:(?P<est_m>\d+)m)?)\s+)?"
    r"(?P<mv>\d+)mv\s+usb\s+(?P<usb_ma>\d+)ma\s+cpu\s+(?P<temp>[\d.]+)\S*C\s+"
    r"chg\s+(?P<chg_ma>\d+)ma"
)


def _parse_battery_line(text: str) -> "dict | None":
    m = _BATTERY_LOG_RE.search(text)
    if not m:
        return None
    if m.group("est"):
        est_h = int(m.group("est_h") or 0)
        est_m = int(m.group("est_m") or 0)
        est_min = est_h * 60 + est_m
    else:
        est_min = None  # no discharge estimate given -- NOT the same as "0 minutes left"
    chg_ma = int(m.group("chg_ma"))
    return {
        "battery_pct": int(m.group("pct")),
        "battery_est_min": est_min,
        "battery_mv": int(m.group("mv")),
        "battery_usb_ma": int(m.group("usb_ma")),
        "battery_charge_ma": chg_ma,
        "battery_charging": chg_ma > 0,
        "battery_cpu_temp_c": float(m.group("temp")),
    }


# Network round-trip check -- also a plain "log" line, confirmed live:
#   "net-chk: ok (25 ms)"
# openspot.py's own _handle_message() already knew about a separate,
# structured "netchk" message TYPE (see the comment there) and deliberately
# left it unsurfaced "in this first pass" -- this is that same signal,
# just read off the human-readable log line instead, the same way battery
# and mode are. Only the "ok (N ms)" success shape has been seen in a real
# capture; anything else (a real failure line's exact wording is
# unconfirmed) is recorded as a non-ok check with no latency rather than
# guessed at.
_NET_CHK_LOG_RE = re.compile(r"net-chk:\s*(?P<status>\S+)(?:\s*\((?P<ms>\d+)\s*ms\))?")


def _parse_net_check_line(text: str) -> "dict | None":
    m = _NET_CHK_LOG_RE.search(text)
    if not m:
        return None
    ok = m.group("status").lower() == "ok"
    ms = int(m.group("ms")) if (ok and m.group("ms")) else None
    return {"net_check_ok": ok, "net_check_ms": ms}


_LEADING_FLOAT_RE = re.compile(r"[\d.]+")


def _parse_leading_float(s) -> "float | None":
    """Pulls the leading number off a value like "32.2°C" -- used for the
    structured "pwr" message's cpu_temp field, which (unlike the log
    line's bare number) arrives as a pre-formatted string with its unit
    already attached."""
    if not s:
        return None
    m = _LEADING_FLOAT_RE.match(str(s))
    return float(m.group()) if m else None


def _format_uptime_pretty(total_seconds) -> str:
    """Mimics `uptime -p`'s wording closely enough for dashboard.html's
    fmtUptime() regex, which just looks for "<N> day"/"<N> hour"/
    "<N> minute" substrings -- singular/plural doesn't matter to it, so
    this doesn't need to reproduce procps' exact grammar rules, just the
    same unit words in the same order, dropping zero-value components."""
    days, rem = divmod(int(total_seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts)


def _mode_for_prefix(prefix: str) -> str:
    if prefix in _MODE_BY_PREFIX:
        return _MODE_BY_PREFIX[prefix]
    return prefix[:-2].upper() if prefix.endswith("ct") else prefix.upper()


def _clean_dst(dst: str) -> str:
    """calllog's "dst" field arrives partially MASKED BY THE DEVICE ITSELF
    for at least some C4FM/YSF group calls -- e.g. "*****FEWaf/127" --
    confirmed live via a real WebSocket capture where the device's own
    free-text "log" line (`"log":"c4fmct: data call started, dst:
    *****FEWaf dgid: 127 src: 9Z3MG ..."`) contains the identical masked
    text, so this isn't a parsing bug on this app's side, nothing more
    can be recovered from it. Cross-checked the masked fragment itself
    against the SAME openSPOT4's own admin call-log page, which listed
    the CALLER (src) as "9Z3MG (Radio ID: FEWaf, ...)" -- i.e. "FEWaf" is
    the caller's own YSF Radio ID leaking into this field, not a
    destination/room name fragment at all, so it's actively misleading
    to display verbatim. The trailing "/<n>" is the one part of this
    field confirmed both real and stable: YSF's DG-ID (Digital Group ID),
    spelled out explicitly in that same device log line ("dgid: 127").
    DMR's dst is a plain talkgroup number with no "*"/"/" in it at all,
    so this is a no-op for DMR -- only reshapes the confirmed-masked
    C4FM/YSF case."""
    if "*" in dst and "/" in dst:
        dgid = dst.rsplit("/", 1)[-1]
        if dgid.isdigit():
            return f"DG-ID {dgid}"
    return dst


def _login(ip: str, password: str) -> str:
    """Full HTTP auth handshake -> a fresh JWT. Endpoint names/shapes
    confirmed live against this firmware (see module docstring)."""
    req = urllib.request.Request(f"http://{ip}{_GETTOK_PATH}")
    with urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT) as resp:
        token = json.loads(resp.read())["token"]

    digest = hashlib.sha256((token + password).encode()).hexdigest()
    body = json.dumps({"token": token, "digest": digest}).encode()
    req = urllib.request.Request(
        f"http://{ip}{_LOGIN_PATH}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())["jwt"]


def _collect_passwords(hotspot: dict) -> list:
    """openSPOT4 config profiles can each have their own separate device
    password (confirmed live), and only one profile is ever active on a
    given physical device at a time. Rather than tracking which profile
    is currently active (there's no API for that -- see CLAUDE.md), just
    collect every password the user has told us about for this device --
    the primary "pass" field plus any extras stored for other profiles --
    and try them all at login time; whichever one matches the currently
    active profile succeeds. Blank/duplicate entries are dropped, order
    preserved (primary first) so the common single-profile case tries
    its one password first, not last."""
    candidates = [hotspot.get("pass", "")]
    extra = hotspot.get("openspot4_extra_pass", "") or ""
    candidates += [line.strip() for line in extra.splitlines()]
    seen = set()
    result = []
    for pw in candidates:
        if pw and pw not in seen:
            seen.add(pw)
            result.append(pw)
    return result


def _login_any(ip: str, passwords: list) -> "tuple[str, int]":
    """Tries each password in order, returning (jwt, index) for the first
    that succeeds. Raises the LAST encountered exception if every one
    fails -- if the device is genuinely unreachable every attempt fails
    the same way regardless of password, and if every attempt is a 401
    (all stored passwords rejected by whichever profile is currently
    active), the last one is just as representative as any other."""
    if not passwords:
        raise ValueError("no password configured")
    last_exc: Exception = ValueError("no password configured")
    for i, pw in enumerate(passwords):
        try:
            return _login(ip, pw), i
        except Exception as e:
            last_exc = e
    raise last_exc


def _describe_login_error(e: Exception, num_passwords: int = 1) -> str:
    """A 401 specifically means every stored password was rejected --
    worth calling out that openSPOT4 config profiles can each carry
    their OWN separate device password (confirmed live: switching
    profiles, which reboots the device into that profile, silently
    invalidated a password that had worked moments before against a
    different profile). Any other error (unreachable/timeout/etc.) is
    left as the plain exception text."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        if num_passwords > 1:
            return (
                f"Login failed: HTTP 401 Unauthorized -- none of the "
                f"{num_passwords} stored passwords were accepted for "
                f"whichever config profile is currently active"
            )
        return (
            "Login failed: HTTP 401 Unauthorized -- wrong password, or the "
            "device is on a different config profile than when this "
            "password was set (each openSPOT4 profile can have its own "
            "separate password -- add its password under \"Additional "
            "profile passwords\" so both are tried automatically)"
        )
    return f"Login failed: {e}"


def _check_token(ip: str, jwt: str) -> None:
    """Cheap pre-flight before the heavier WS handshake -- confirmed live:
    GET /checktok + Authorization: Bearer <jwt> -> 200 for a valid token.
    Raises on any non-200."""
    req = urllib.request.Request(
        f"http://{ip}{_CHECKTOK_PATH}", headers={"Authorization": f"Bearer {jwt}"}
    )
    urllib.request.urlopen(req, timeout=config.OPENSPOT4_HTTP_TIMEOUT).close()


class _OpenSpot4Worker:
    """One persistent WebSocket connection to one openSPOT4 device.
    Unlike aprs_inbox.py's reconnect (same static passcode every time), a
    reconnect here must redo the full HTTP auth handshake (fresh token ->
    digest -> login -> new JWT) before reopening the WS, since a JWT
    can't just be reused indefinitely."""

    def __init__(self, hotspot: dict, monitor):
        self._ip = hotspot["ip"]
        self._passwords = _collect_passwords(hotspot)
        self._monitor = monitor
        self._stop_event = threading.Event()
        self._active_call: dict | None = None  # {"id","src","dst"} for the one in-flight call this device is tracking
        # Confirmed live: right after the WS opens, the device replays
        # several PAST calllog entries (src=="openSPOT4" -- its own
        # identity, not a real caller -- for the ones observed so far, but
        # not assumed to always be, see _on_calllog), terminated by a
        # {"type":"calllogend"} marker. Until that marker arrives on THIS
        # connection, calllog entries are history, not new events -- see
        # _on_calllog's own guard. Reset per-connection in _run_once(),
        # same as _active_call above.
        self._replay_done = False
        # calllog entries get rebroadcast periodically even after a call has
        # ended (observed live: the same id/duration pair re-appearing
        # ~10s later, unchanged) -- dedupe so a rebroadcast doesn't
        # re-trigger "call ended" processing (re-logging Fleet activity,
        # re-touching last_heard, etc.) every time it re-arrives.
        self._finalized_call_ids: deque = deque(maxlen=50)
        # DMR ID (str) -> (callsign, name), bounded LRU-ish cache. Real,
        # previously-shipped bug found from a full real call-cycle capture:
        # "csd" (the message that resolves a DMR ID to a callsign) arrived
        # BEFORE the matching calllog "call started" event, both at the
        # call's start AND again near its end -- the "csd: dmr id ... query
        # took 60ms" log line confirms the device kicks off the ID lookup
        # the instant it sees the call start internally, ahead of sending
        # either JSON message to this app. The original _on_csd only ever
        # applied its enrichment if self._active_call was ALREADY set, so
        # that first, early csd was silently dropped every time -- the
        # caller's real name/callsign wouldn't show up until a SECOND csd
        # happened to arrive later (in the captured example: not until
        # 06:56:45.629, for a call that started at 06:56:34.995 and ended
        # at 06:56:45.691 -- the bare DMR ID would have shown for nearly
        # the entire ~10.7s call). Fixed by caching every resolved DMR ID
        # here unconditionally
        # (see _on_csd), and having _apply_call_start check this cache
        # immediately when a call starts, regardless of which message won
        # the race.
        self._recent_csd: dict = {}
        # openSPOT4's own built-in APRS-IS messaging ("APRS chat") --
        # bounded recent-activity log, persists across reconnects (not
        # reset in _run_once, same as _finalized_call_ids/_recent_csd
        # above -- it's a log of past activity, not per-connection state).
        # _aprs_pending_acks maps a pending OUTBOUND message's own "id"
        # (confirmed live, e.g. "00001") to the SAME dict object stored in
        # _aprs_messages, so a later matching aprstxmsggotack can flip
        # that entry's "acked" flag in place via O(1) lookup rather than
        # a linear scan, without leaking the correlation id itself into
        # the public-facing dict.
        self._aprs_messages: deque = deque(maxlen=20)
        self._aprs_pending_acks: dict = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def config_matches(self, hotspot: dict) -> bool:
        return (hotspot.get("ip") == self._ip
                and _collect_passwords(hotspot) == self._passwords)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception:
                self._monitor.mark_external_offline(self._ip)
            if not self._stop_event.is_set():
                self._stop_event.wait(config.OPENSPOT4_RECONNECT_BACKOFF)

    def _run_once(self) -> None:
        jwt, _ = _login_any(self._ip, self._passwords)
        _check_token(self._ip, jwt)  # fail fast with a clear auth error before the WS upgrade
        ws = websocket.create_connection(
            f"ws://{self._ip}/{jwt}",
            subprotocols=[_WS_SUBPROTOCOL],
            timeout=config.OPENSPOT4_RECV_TIMEOUT,
        )
        try:
            self._active_call = None
            self._replay_done = False
            while not self._stop_event.is_set():
                raw = ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                self._handle_message(msg)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # --- message dispatch ---

    def _handle_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "status":
            self._on_status(msg)
        elif mtype == "log":
            self._on_log(msg.get("log", ""))
        elif mtype == "csd":
            self._on_csd(msg)
        elif mtype == "calllog":
            self._on_calllog(msg)
        elif mtype == "calllogend":
            self._on_calllogend(msg)
        elif mtype == "pwr":
            self._on_pwr(msg)
        elif mtype == "wifirssi":
            self._on_wifirssi(msg)
        elif mtype == "resp":
            self._on_resp(msg)
        elif mtype == "time":
            self._on_time(msg)
        elif mtype == "connectedto":
            self._on_connectedto(msg)
        elif mtype == "netstate":
            self._on_netstate(msg)
        elif mtype == "aprsbgstate":
            self._on_aprsbgstate(msg)
        elif mtype == "aprsmsg":
            self._on_aprsmsg(msg)
        elif mtype == "aprstxmsgwaitack":
            self._on_aprstxmsgwaitack(msg)
        elif mtype == "aprstxmsggotack":
            self._on_aprstxmsggotack(msg)
        # "cptimeouts" IS confirmed live now ({"type":"cptimeouts","cp":0,
        # "cp_sec":0,"null_sec":0,"null_pdown":0}, periodic, always zero
        # so far) -- still not surfaced, since every field name is a
        # cryptic abbreviation and every real sample has been all-zero,
        # so there's no contrasting value to confirm what any of them
        # actually mean. "netchk" isn't a separate message type at all as
        # far as any real capture has shown -- it only ever appears as a
        # "net-chk: ok (N ms)" LOG line (see _NET_CHK_LOG_RE in _on_log),
        # unlike "pwr"/"wifirssi" which are confirmed to arrive BOTH as a
        # "log" text line and as their own structured "type" message.
        # A handful of other real, confirmed-live message types
        # (state/modemmode/connector/tmpnetconnstate/pocsagstate/
        # pocsagmsgqueue/dapnetbgstate/fwuinfo/fwustate) are still
        # deliberately not handled -- device-internal state, disabled
        # features (POCSAG/DAPNET background state were both -1 in every
        # real sample -- unlike "aprsbgstate", which turned out to mean
        # something narrower than "is APRS enabled" and IS handled now,
        # see _on_aprsbgstate), or one-shot boot-time noise, none with an
        # established dashboard need yet.

    def _on_resp(self, msg: dict) -> None:
        """"resp" wraps MANY different query responses (rx/tx frequency,
        homebrew/bmm settings, etc.), correlated by a per-request "id" that
        is NOT stable across connections -- confirmed live, the same
        cp_names/active_cp shape arrived under two different id values
        (62345, then 7703) on two separate page loads. So this reacts to
        the specific SHAPE of the response ("cp_names" present), not any
        fixed id.
        Confirmed live, arriving UNSOLICITED as part of a batch of ~11
        "resp" messages the device pushes automatically right after the WS
        opens -- this worker never sends a "get" for it and still receives
        it, so no outbound message is needed on this app's side:
          {"type":"resp","id":62345,"resp":{"reboot":0,"active_cp":0,
           "timeoutchange_cp":0,"rxtimeout_sec":0,"rxtimeout_mode":0,
           "active_cp_hostname":"openspot4","cp_names":["Brandmeister",
           "TGIF","YSF","profile #4","profile #5","profile #6",
           "profile #7","profile #8","profile #9","profile #10"],
           "always_boot_p0":0},"code":200}
        "active_cp" is a 0-indexed position into cp_names -- confirmed
        against the device's OWN admin UI, which showed "Active config
        profile: 1 (Brandmeister)" for this exact active_cp:0/
        cp_names[0]=="Brandmeister" state (the UI displays the 1-indexed
        position, +1 over the raw value)."""
        resp = msg.get("resp")
        if not isinstance(resp, dict) or "cp_names" not in resp:
            return
        names = resp.get("cp_names") or []
        idx = resp.get("active_cp")
        if not isinstance(idx, int) or not (0 <= idx < len(names)):
            return
        self._monitor.apply_external_update(self._ip, {
            "active_config_profile_num": idx + 1,
            "active_config_profile_name": names[idx],
        })

    def _on_calllogend(self, _msg: dict) -> None:
        """Marks the end of the connect-time calllog HISTORY replay (see
        _on_calllog's own docstring for the bug this fixes) -- everything
        received after this point is a genuinely live event."""
        self._replay_done = True

    def _on_connectedto(self, msg: dict) -> None:
        """Confirmed live: {"type":"connectedto","to":"YSF 32592",
        "server":"americalink.radiotechnology.xyz","primary":1,
        "read_only":0}. Tells us what reflector/room/master this device
        is CURRENTLY CONNECTED TO, independent of whether a call is
        active -- something openSPOT4 had no equivalent of on this
        dashboard before. Deliberately kept separate from `talkgroup`
        (set by _apply_call_start, call-scoped and can go briefly stale
        after a call ends since _on_calllog's final branch doesn't touch
        it) -- this is the persistent connector-level link, not a
        specific call's destination, same distinction DMR's static
        Brandmeister talkgroup link (app.py's bm_static_tgs) has versus
        its own live talkgroup."""
        to = msg.get("to")
        if not to:
            return
        self._monitor.apply_external_update(self._ip, {
            "connector_target": to,
            "connector_server": msg.get("server"),
        })

    def _on_netstate(self, msg: dict) -> None:
        """Confirmed live: {"type":"netstate","data":{"connected":1,
        "ap_mode":0,"ssid":"Sanctuary24-5g","bssid":"18:e8:29:c4:4e:11",
        "chnr":11,"sec":"wpa2","phy":"bgn","static_ip":0,
        "ip":"192.168.156.242","mask":"255.255.255.0","gw":"...",
        "ip6":[],"ip6_enabled":0,"static_dns":0,"dns1":"...","dns2":"...",
        "regdom":"FCC"}}. Only "ssid" is surfaced for now -- gives the
        bare wifi_rssi_dbm number actual context (which network it's
        even measuring). The rest (IP/gateway/DNS/regdom/security) is
        real but not surfaced -- no established dashboard need for it
        beyond this."""
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        ssid = data.get("ssid")
        if not ssid:
            return
        self._monitor.apply_external_update(self._ip, {"wifi_ssid": ssid})

    def _on_aprsbgstate(self, msg: dict) -> None:
        """openSPOT4's OWN built-in APRS-IS connection state (the "APRS
        chat" feature). Confirmed live across a real connect cycle, in
        order: -1 right before "aprs: task terminating" (disconnected),
        0 right as "aprs: init (callsign: ... server: ...)" fires
        (connecting), 2 right after "aprs: connected to <host>:<port>
        (<ip>)" (connected). Other raw values not seen -- dropped rather
        than guessed at. Entirely separate from this app's OWN
        aprs_inbox.py/aprs_messaging.py, which maintain their own
        independent APRS-IS session under settings.json's own
        credentials -- this is the DEVICE's own connection, not this
        dashboard's."""
        label = _APRS_BGSTATE_LABELS.get(msg.get("state"))
        if label is None:
            return
        self._monitor.apply_external_update(self._ip, {"aprs_conn_state": label})

    def _record_aprs_update(self) -> None:
        self._monitor.apply_external_update(self._ip, {"aprs_messages": list(self._aprs_messages)})

    def _on_aprsmsg(self, msg: dict) -> None:
        """Confirmed live -- an inbound APRS message (a reply from WXBOT,
        a weather-query bot, in the one real sample captured so far):
          {"type":"aprsmsg","msg":{"is_outbound":0,"is_unconfirmed":0,
           "callsign":"WXBOT","msg":"Manchester NH. Today,Sunny High 81",
           "id":"","ts":1786791746}}
        NOT confirmed whether this also fires for an unsolicited message
        from someone who isn't replying to something sent first -- the
        only real sample so far is a direct reply to an outbound query."""
        inner = msg.get("msg")
        if not isinstance(inner, dict) or inner.get("is_outbound"):
            return
        callsign = inner.get("callsign")
        text = inner.get("msg")
        if not callsign or text is None:
            return
        self._aprs_messages.append({
            "direction": "in", "callsign": callsign, "text": text,
            "ts": inner.get("ts"), "acked": None,
        })
        self._record_aprs_update()

    def _on_aprstxmsgwaitack(self, msg: dict) -> None:
        """Confirmed live -- fires right after WE send a message, while
        the device waits for the recipient's ack:
          {"type":"aprstxmsgwaitack","msg":{"is_outbound":1,
           "is_unconfirmed":0,"callsign":"WXBOT","msg":"Lincoln NH",
           "id":"00001","ts":1786791740},"remaining_sec":30}
        "remaining_sec" (a countdown to giving up) isn't tracked -- not
        useful on a several-second dashboard poll cadence."""
        inner = msg.get("msg")
        if not isinstance(inner, dict):
            return
        callsign = inner.get("callsign")
        text = inner.get("msg")
        msg_id = inner.get("id")
        if not callsign or text is None:
            return
        entry = {
            "direction": "out", "callsign": callsign, "text": text,
            "ts": inner.get("ts"), "acked": False,
        }
        self._aprs_messages.append(entry)
        if msg_id:
            self._aprs_pending_acks[msg_id] = entry
            if len(self._aprs_pending_acks) > 20:
                self._aprs_pending_acks.pop(next(iter(self._aprs_pending_acks)))
        self._record_aprs_update()

    def _on_aprstxmsggotack(self, msg: dict) -> None:
        """Confirmed live: {"type":"aprstxmsggotack","id":"00001"} --
        "id" matches the "id" field inside an earlier aprstxmsgwaitack's
        own "msg" dict. Flips that SAME entry's acked flag in place
        (via _aprs_pending_acks' shared object reference) rather than
        appending a new row -- an ack isn't a new message."""
        msg_id = msg.get("id")
        entry = self._aprs_pending_acks.pop(msg_id, None) if msg_id else None
        if entry is None:
            return  # no matching pending entry (e.g. worker started mid-flight) -- nothing to update
        entry["acked"] = True
        self._record_aprs_update()

    def _on_time(self, msg: dict) -> None:
        """Confirmed live:
          {"type":"time","now":1786790012,"up":2307,
           "ntp_last_synced_at":1786787710,"ntp_synced_host":"pool.ntp.org",
           "tzoffset":-14400}
        "up" is the device's own uptime in seconds -- fills a real gap,
        since openSPOT4 has no SSH and its uptime badge/drawer stat has
        never had any data source before this. Formatted into the SAME
        shape monitor.py's WPSD/ASL3 paths already store on `uptime`
        (Linux `uptime -p`'s wording, minus its leading "up " -- see
        config._LINUX_HOST_STATS_CMD / monitor.py's `output[1].replace
        ("up ", "")`), so this reuses every existing `hs.uptime` consumer
        (the card's own uptime badge, fmtUptime(), the drawer's fallback
        header stats row) with no changes needed there. Only "now"/
        "ntp_*"/"tzoffset" are ignored -- this app already has its own
        server clock, and NTP sync health isn't worth surfacing yet
        without a reported need."""
        up = msg.get("up")
        if not isinstance(up, (int, float)):
            return
        self._monitor.apply_external_update(self._ip, {"uptime": _format_uptime_pretty(up)})

    def _on_status(self, msg: dict) -> None:
        status = msg.get("status") or {}
        updates = {}
        rssi_vals = status.get("rssi_values_dbm") or []
        ber_vals = status.get("ber_values") or []
        if rssi_vals:
            updates["rssi"] = f"{rssi_vals[-1]} dBm"
        if ber_vals:
            updates["ber"] = f"{ber_vals[-1]}%"
        # Even an empty heartbeat is proof of life -- always touch the
        # monitor so the failure counter resets / status stays Online.
        self._monitor.apply_external_update(self._ip, updates)

    def _on_pwr(self, msg: dict) -> None:
        """Confirmed live as a genuine structured message, e.g.:
          {"type":"pwr","detected":1,"charging":0,"charge_heat_err":0,
           "fault":0,"low_curr":0,"curr_ma":1500,"percent":100,"mv":4149,
           "cpu_temp":"32.2°C","remaining_min":0}
        Strictly better than the "pwr: batt ..." log-line scrape
        (_parse_battery_line) -- real booleans, no regex, and it exposes
        fault/low_curr/charge_heat_err diagnostics the log line doesn't
        have at all -- but kept as an ADDITIONAL source alongside the log
        line rather than a replacement, since only one real sample of this
        message has been captured so far and it's unconfirmed whether
        every firmware build emits it. Both target the same battery_*
        fields; if both fire for the same tick it's just a harmless
        redundant write of identical values.
        Note: "curr_ma" matches the log line's "usb <n>ma" (input current),
        NOT its separate "chg <n>ma" (charge current) -- this message has
        no equivalent of that second figure, so battery_charge_ma is left
        untouched here rather than guessed from curr_ma.
        remaining_min was 0 in the one real sample, on a fully charged
        (100%), non-charging unit -- treated the same as the log line's
        own "est. clause absent" case (None, not "0 minutes left"). NOT
        yet confirmed what this looks like while genuinely discharging
        with a real estimate -- re-verify against a low-battery capture
        if one ever turns up.
        """
        if "percent" not in msg:
            return
        remaining = msg.get("remaining_min")
        updates = {
            "battery_pct": msg.get("percent"),
            "battery_est_min": remaining if remaining else None,
            "battery_mv": msg.get("mv"),
            "battery_usb_ma": msg.get("curr_ma"),
            "battery_charging": bool(msg.get("charging")),
            "battery_cpu_temp_c": _parse_leading_float(msg.get("cpu_temp")),
        }
        if "detected" in msg:
            updates["battery_detected"] = bool(msg.get("detected"))
        if "fault" in msg:
            updates["battery_fault"] = bool(msg.get("fault"))
        if "low_curr" in msg:
            updates["battery_low_curr"] = bool(msg.get("low_curr"))
        self._monitor.apply_external_update(self._ip, updates)

    def _on_wifirssi(self, msg: dict) -> None:
        """Confirmed live, twice: {"type":"wifirssi","dbm":-69,"apclient":0}
        and {"type":"wifirssi","dbm":-68,"apclient":0}. "dbm" is
        unambiguous (WiFi signal strength). "apclient"'s exact meaning is
        NOT confirmed -- both real samples had it 0, so there's no
        contrasting value to infer from yet -- passed through raw rather
        than interpreted as a bool/count with unproven semantics. Only
        ever expected on a unit actually connected over WiFi; a
        wired/Ethernet unit presumably never emits this at all, same
        "absence means not applicable" convention as battery_* above."""
        if "dbm" not in msg:
            return
        self._monitor.apply_external_update(self._ip, {
            "wifi_rssi_dbm": msg.get("dbm"),
            "wifi_ap_client": msg.get("apclient"),
        })

    def _on_log(self, text: str) -> None:
        """Three independent things get opportunistically read off "log"
        lines, none of which is the call-lifecycle signal (that's
        _on_calllog): the human mode name (DMR/YSF/...) from whichever
        mode's own "<x>ct:" log line happens to show up; on a
        battery-powered unit, a "pwr: batt ..." line (see
        _BATTERY_LOG_RE); and a periodic "net-chk: ok (N ms)" round-trip
        check (see _NET_CHK_LOG_RE). Deliberately doesn't try to parse
        anything past the mode prefix for the mode case (dst/src/dur/ber/
        etc. all come from calllog instead, in a consistent shape across
        modes, unlike these log lines which differ per mode -- confirmed
        live, DMR's has a "[N]" channel bracket and an explicit "call
        started" line, C4FM's has neither)."""
        m = _MODE_LOG_RE.match(text)
        if m:
            self._monitor.apply_external_update(self._ip, {"mode": _mode_for_prefix(m.group(1))})
            return
        battery = _parse_battery_line(text)
        if battery:
            self._monitor.apply_external_update(self._ip, battery)
            return
        net_check = _parse_net_check_line(text)
        if net_check:
            self._monitor.apply_external_update(self._ip, net_check)

    def _on_calllog(self, msg: dict) -> None:
        """The universal call-lifecycle signal, confirmed live across two
        different modes/connectors (DMR/Homebrew and C4FM/YSF Reflector):
        a "calllog" entry with duration 0.0 is a call starting; the SAME
        "id" reappearing later with a real duration is that call ending.
        This is a structured JSON shape that's consistent across modes,
        unlike the "log" text lines this replaced for call tracking.

        A REAL, previously-shipped bug, caught from a fresh real capture:
        right after the WS opens, the device replays several PAST calllog
        entries (confirmed live -- src=="openSPOT4", the device's own
        identity, not a caller; short durations; terminated by a
        {"type":"calllogend"} marker) before switching to genuinely live
        events. The original code had no way to tell these apart from a
        real call just ending, so EVERY worker restart/reconnect would
        spuriously re-log stale historical activity with a "just
        happened" timestamp -- wrong Fleet Activity entries, last_heard
        reset to the reconnect time instead of staying at whatever it
        already was. Fixed by gating on self._replay_done (set True by
        _on_calllogend) -- entries seen before that marker are recorded
        into _finalized_call_ids (so a post-replay rebroadcast of the
        same id, which calllog entries are already known to do, doesn't
        slip through as new) but never applied to the dashboard."""
        call_id = msg.get("id")
        if not call_id:
            return
        src = str(msg.get("src") or "")
        dst = str(msg.get("dst") or "")
        duration = msg.get("duration")
        is_final = isinstance(duration, (int, float)) and duration > 0

        if not self._replay_done:
            if is_final:
                self._finalized_call_ids.append(call_id)
            return

        if not is_final:
            if self._active_call and self._active_call["id"] == call_id:
                return  # already tracking this one -- a rebroadcast of the same "started" state
            self._active_call = {"id": call_id, "src": src, "dst": dst}
            self._apply_call_start(src, dst)
            return

        if call_id in self._finalized_call_ids:
            return  # already processed this call's ending -- just a periodic rebroadcast
        self._finalized_call_ids.append(call_id)

        ber = msg.get("ber")
        rssi = msg.get("rssi")
        updates = {"is_active": False, "tx_start": None, "last_heard": time.time()}
        if isinstance(ber, (int, float)) and ber >= 0:
            updates["ber"] = f"{ber}%"
        if isinstance(rssi, (int, float)):
            updates["rssi"] = f"{rssi} dBm"
        self._monitor.apply_external_update(self._ip, updates)
        self._monitor.log_activity(self._ip)
        if self._active_call and self._active_call["id"] == call_id:
            self._active_call = None

    def _apply_call_start(self, src: str, dst: str) -> None:
        is_fav, fav_label = self._monitor.apply_favorite_match(src)
        self._monitor.apply_external_update(self._ip, {
            "is_active": True,
            "active_call": src,  # DMR ID placeholder (csd resolves it) or already a real callsign
            "talkgroup": _clean_dst(dst),
            "tx_start": time.time(),
            "last_heard": None,
            "is_favorite": is_fav,
            "favorite_label": fav_label,
        })
        if not src.isdigit():
            # Not a DMR ID -- src is already a real callsign (confirmed
            # live for C4FM/YSF, which addresses stations by callsign
            # directly rather than a numeric ID), so there's no separate
            # "csd" resolution coming the way DMR gets one. Enrich it
            # here directly instead, same QRZ/RadioID/APRS path _on_csd
            # uses for DMR.
            self._enrich_caller(src)
        elif src in self._recent_csd:
            # This exact DMR ID was already resolved by a csd that arrived
            # BEFORE this call started (the confirmed real race -- see
            # _recent_csd's own comment) -- apply it immediately instead
            # of waiting for a possible later csd re-arrival.
            callsign, name = self._recent_csd[src]
            self._enrich_caller(callsign, name)

    def _on_csd(self, msg: dict) -> None:
        if msg.get("id_type") != "dmr":
            return
        dmr_id = str(msg.get("id"))
        callsign = msg.get("callsign")
        if not callsign:
            return
        name = msg.get("name")
        # Cache UNCONDITIONALLY (not gated on an active call already being
        # tracked) -- see _recent_csd's own comment for why: this message
        # is known to sometimes arrive before the calllog "call started"
        # event it belongs to, and a DMR ID -> callsign mapping is a
        # stable fact worth remembering regardless of timing.
        self._recent_csd[dmr_id] = (callsign, name)
        if len(self._recent_csd) > 20:
            self._recent_csd.pop(next(iter(self._recent_csd)))
        active = self._active_call
        if active and active["src"] == dmr_id:
            self._enrich_caller(callsign, name)

    def _enrich_caller(self, call: str, device_name: str = None) -> None:
        """Shared caller-info enrichment for both DMR (via _on_csd, keyed
        off a resolved callsign) and C4FM/YSF (via _apply_call_start,
        already a callsign with no separate resolution step). openSPOT's
        own DMR-ID lookup (or C4FM/YSF's own callsign addressing) only
        gives a callsign+name, not location/photo/position -- reuse the
        same QRZ/RadioID/APRS enrichment path WPSD/ASL3 use for that. The
        device's own name (when given, DMR only) still takes priority
        over QRZ/RadioID's (fresher, a per-call lookup done by the
        device itself)."""
        caller_info = self._monitor.lookup_caller_info(call)
        resolved_name = device_name or caller_info["name"]
        is_fav, fav_label = self._monitor.apply_favorite_match(call)

        prior = self._monitor.snapshot().get(self._ip, {})
        history = list(prior.get("history", []))
        updates = {
            "active_call": call,
            "caller_name": resolved_name,
            "caller_location": caller_info["location"],
            "caller_image": caller_info["image_url"],
            "caller_lat": caller_info["lat"],
            "caller_lon": caller_info["lon"],
            "caller_source": caller_info["source"],
            "is_favorite": is_fav,
            "favorite_label": fav_label,
        }
        if not any(h.get("call") == call for h in history):
            history.insert(0, {
                "call": call, "name": resolved_name, "location": caller_info["location"],
                "lat": caller_info["lat"], "lon": caller_info["lon"], "source": caller_info["source"],
            })
            updates["history"] = history[:config.MAX_HISTORY]
        self._monitor.apply_external_update(self._ip, updates)


class OpenSpot4Manager:
    """Reconciles hotspots.json's type=="openspot4" entries against live
    per-device worker threads. Same shape as camera_stream.py's
    CameraStreamManager (dict of workers + lock + explicit stop/remove) --
    aprs_inbox.py's generation-counter trick isn't needed here since
    removal is explicit/synchronous (worker.stop()), not "supersede a
    stale thread." Eagerly started (not lazy-on-first-viewer, unlike
    camera workers) since this is a push source for the dashboard, not a
    viewer-driven stream."""

    def __init__(self, monitor):
        self._monitor = monitor
        self._lock = threading.Lock()
        self._workers: dict[str, _OpenSpot4Worker] = {}

    def reconcile(self, hotspots: list) -> None:
        # A disabled hotspot stays in hotspots.json (config/creds retained)
        # but is excluded here, so its worker gets torn down like any
        # other removed device -- same "keep config, stop polling" pattern
        # FleetMonitor's own run_forever()/run_slow_checks_forever() use.
        desired = {
            h["ip"]: h for h in hotspots
            if h.get("type") == "openspot4" and h.get("enabled", True)
        }
        to_stop = []
        with self._lock:
            for ip, worker in list(self._workers.items()):
                hs = desired.get(ip)
                if hs is None or not worker.config_matches(hs):
                    to_stop.append(self._workers.pop(ip))
            for ip, hs in desired.items():
                if ip not in self._workers:
                    worker = _OpenSpot4Worker(hs, self._monitor)
                    worker.start()
                    self._workers[ip] = worker
        for worker in to_stop:  # outside the lock -- mirrors CameraStreamManager
            worker.stop()

    def remove(self, ip: str) -> None:
        with self._lock:
            worker = self._workers.pop(ip, None)
        if worker is not None:
            worker.stop()

    def run_forever(self) -> None:
        """Periodic safety-net reconcile (belt-and-suspenders, same
        rationale as monitor.py's prune_stale) -- every route that
        mutates hotspots.json already calls reconcile() explicitly, so
        this loop mostly no-ops in practice."""
        while True:
            self.reconcile(load_hotspots())
            time.sleep(config.POLL_INTERVAL)

    def test_connection(self, ip: str, password: str, extra_passwords: str = "") -> tuple[bool, str]:
        """Settings 'Test' button -- login + checktok only, no persistent
        WS opened (mirrors /api/test_asl_node's non-persistent smoke test).
        extra_passwords is the raw multi-line textarea value (other config
        profiles' passwords) -- tried in order alongside the primary one,
        same as the real worker does."""
        passwords = _collect_passwords({"pass": password, "openspot4_extra_pass": extra_passwords})
        try:
            jwt, idx = _login_any(ip, passwords)
        except Exception as e:
            return False, _describe_login_error(e, len(passwords))
        try:
            _check_token(ip, jwt)
        except Exception as e:
            return False, f"Token check (checktok) failed: {e}"
        which = "primary password" if idx == 0 else f"additional password #{idx}"
        return True, f"Login succeeded using the {which}"
