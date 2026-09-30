"""Receives APRS text messages addressed to your callsign via APRS-IS --
the receiving half of aprs_messaging.py's sending. Same underlying
mechanism (aprslib, no API key/account signup, passcode is a deterministic
checksum of your callsign), just a persistent listening connection instead
of a connect-send-disconnect one, since messages can arrive at any time.

Verified against aprslib's actual source before writing this (not just its
docs), same discipline as aprs_messaging.py: a synthetic message packet
parsed with the real library produces
    {"from": ..., "addresse": ..., "format": "message",
     "message_text": ..., "msgNo": ...}
(note aprslib's own spelling, "addresse" not "addressee"). Ack/reject
packets (someone acking a message *we* sent) also come back with
format == "message" but without message_text -- that's the field this
module uses to tell "a message for me to show and ack" apart from
"an ack/reject of something I sent", not the format field alone.

Filter uses APRS-IS's documented "buddy" filter (`b/CALLSIGN*`), which
per the javAPRSFilter spec's own wording ("Pass all traffic from exact
call: call1, call2, ... (* wild card allowed)") defaults to an EXACT
match with no SSID wildcarding -- confirmed against the real spec, not
assumed. `b/W1ZLA` alone would only catch messages to literally "W1ZLA",
missing "W1ZLA-9", "W1ZLA-1", etc. -- the same way apps like APRS.fi
aggregate messages across every SSID of your callsign, this wildcards on
the base call (`b/W1ZLA*`) so any SSID variant is caught. The base call
is used for both the filter AND the packet-match check in _on_packet --
widening only one side would either leak nothing (if the match stayed
exact) or accept the whole APRS-IS firehose (if the filter were dropped
instead of widened).

The listening connection logs in UNVERIFIED (passcode "-1"), not with the
real computed passcode -- confirmed live against the real network, not
assumed: a connection logged in VERIFIED as a callsign gets disconnected
by APRS-IS the instant a SECOND connection also logs in VERIFIED as that
same callsign, which is exactly what happens every time
aprs_messaging.py's own send (or this module's own _send_ack) opens its
short-lived verified connection to actually transmit something. This was
a real, reported bug -- "test message doesn't show up in the inbox" --
traced to the listener getting kicked and only reconnecting ~15s later
(RECONNECT_BACKOFF), after the message had already come and gone. Fixed
by logging the LISTENER in unverified, since it only ever needs to
receive; verified status only gates transmit permission, and filtering
behavior is identical either way (confirmed live: an unverified b/W1ZLA*
listener received a packet sent by a separate verified W1ZLA connection
without being disconnected). A bigger fix was seriously considered first
-- routing all sends through this module's own persistent connection
instead -- but aprslib's IS.sendall() and its consumer() read loop both
mutate the same socket's blocking mode with no locking between them
(confirmed by reading aprslib/IS.py directly), so calling sendall() from
another thread while consumer() blocks in this module's own thread is a
real, unguarded race. Don't reach for that shared-connection design for
this bug -- the verified-vs-verified collision was the actual constraint,
not the number of connections.

Backfill (catching up on messages sent while this listener was down):
APRS-IS has no history, so a message sent during a restart/rebuild/
dropped connection is simply gone from the live feed. aprs.fi keeps
them, via its documented messages query (https://aprs.fi/page/api,
read directly, 2026-09):
    GET https://api.aprs.fi/api/get?what=msg&dst=CALL1,CALL2&apikey=K&format=json
    -> {"result":"ok","entries":[{"messageid","time","srccall","dst","message"}]}
Documented limits that shape this code: at most the 10 LATEST messages,
up to 10 recipients per call, and EXACT callsigns only (no b/CALL*
wildcard like the live filter) -- so the callsigns checked are the
configured one, the base call, and every SSID messages have actually
arrived on (from the saved history), capped at 10. The response has no
msgNo, so a caught-up message can't be acked (the sender stopped retrying
long ago anyway) and is stored with acked=False, backfilled=True.
When: after a reconnect whose gap (since the last "still connected"
heartbeat, persisted in aprs_backfill.json so a restart counts too) is
over BACKFILL_MIN_GAP, at most once per BACKFILL_MIN_INTERVAL. A gap
skipped by that throttle stays pending and is picked up by the heartbeat
thread once allowed, so a flapping connection loses nothing. Only
messages timestamped inside the gap are imported; anything whose
messageid was already imported, or that matches a message the live
listener stored (same sender/addressee/text within 10 min), is skipped.
A successful catch-up that found something records a "gap" entry
(offline duration, count, maybe_incomplete when aprs.fi's 10-message cap
was hit inside the gap) shown as a divider in the Notifications card.
"""
import json
import os
import re
import time
import threading
import urllib.parse
import urllib.request
from collections import deque

import aprslib

import config
import storage_notifications

APRS_IS_HOST = "rotate.aprs2.net"
APRS_IS_PORT = 14580
MAX_MESSAGES = 50
RECONNECT_BACKOFF = 15

APRSFI_URL = "https://api.aprs.fi/api/get"
APRSFI_MAX_DST = 10          # documented: up to 10 recipients per call
APRSFI_MAX_ENTRIES = 10      # documented: at most the 10 latest messages
BACKFILL_MIN_GAP = 60        # seconds offline before a catch-up is worth doing
BACKFILL_MIN_INTERVAL = 300  # at most one aprs.fi call per 5 minutes
BACKFILL_DEDUPE_WINDOW = 600 # a live message within 10 min with the same text is the same message
HEARTBEAT_TICK = 15
HEARTBEAT_PERSIST = 60
MAX_GAPS = 10
STATE_FILE = os.path.join(config.CONFIG_DIR, "aprs_backfill.json")
_ACK_RE = re.compile(r"^(ack|rej)[A-Za-z0-9]{1,5}$")

# Sending (the Notifications card's composer). Each outbound message
# carries a message number and is retried until an ack arrives on this
# listener's connection -- aprslib parses an incoming ack/rej as
# {"format": "message", "response": "ack"|"rej", "msgNo": ...} with no
# message_text (confirmed against the installed library).
SEND_MAX_TEXT = 67           # APRS message text limit (APRS101)
SEND_TRIES = 3
SEND_RETRY_SEC = 30
_BAD_TEXT_CHARS = re.compile(r"[^\x20-\x7e]|[{|~]")  # printable ASCII only; { | ~ are reserved in message text


def _clean_callsign(callsign: str) -> str:
    return re.sub(r"\s+", "", (callsign or "")).upper()


def _base_call(callsign: str) -> str:
    """Strips a trailing "-N" SSID, e.g. "W1ZLA-9" -> "W1ZLA". Used to
    match/filter across every SSID of a callsign, the same way APRS.fi
    aggregates messages regardless of which specific SSID they were sent to."""
    return re.sub(r"-\d+$", "", _clean_callsign(callsign))


def _addressee_field(callsign: str) -> str:
    return callsign[:9].ljust(9)


class AprsInbox:
    """One persistent background APRS-IS connection, reconfigured (not
    recreated) when settings change via configure() -- mirrors the
    rebuild-on-settings-save pattern used for the other integration
    clients, but as a long-lived listener rather than a stateless client
    object, since a listening socket can't just be discarded and remade
    per-call the way a QRZ/Brandmeister lookup client can."""

    def __init__(self):
        self._lock = threading.Lock()
        self._callsign = ""
        self._enabled = False
        self._generation = 0  # bumped on every configure() so a stale thread's loop exits cleanly
        self._state = "disabled"  # disabled | connecting | connected | reconnecting
        self._messages = deque(maxlen=MAX_MESSAGES)
        self._acked = deque(maxlen=200)  # (from, msgNo) pairs already acked, dedupes retries
        self._thread = None
        # Reseed from disk so a restart doesn't lose message history --
        # oldest-first replay through the same appendleft() a live message
        # uses reproduces the exact newest-first deque state live traffic
        # would have built up (see storage_notifications.recent()).
        for payload in storage_notifications.recent("aprs", MAX_MESSAGES):
            # a send still in progress when the process stopped will never
            # finish its retries -- say so rather than "waiting" forever
            if payload.get("outbound") and payload.get("status") in ("sending", "waiting"):
                payload["status"] = "no_ack"
            self._messages.appendleft(payload)
        self._msg_counter = int(time.time()) % 90000

        # --- backfill state ---
        self._backfill_enabled = False
        self._api_key = ""
        self._gaps = deque(maxlen=MAX_GAPS)  # newest first, same as _messages
        for payload in storage_notifications.recent("aprs_gap", MAX_GAPS):
            self._gaps.appendleft(payload)
        self._last_alive = None        # last moment the listener was known connected
        self._pending_since = None     # start of a gap not yet caught up
        self._last_backfill_at = 0.0   # in-memory: the 5-min throttle restarts with the process
        self._backfill_running = False
        self._backfill_status = None   # {"at","found","error","callsigns"} of the last attempt
        self._seen_ids = deque(maxlen=300)
        self._last_persist = 0.0
        self._heartbeat = None
        self._load_state()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def configure(self, callsign: str, enabled: bool) -> None:
        callsign = _clean_callsign(callsign)
        with self._lock:
            changed = (callsign != self._callsign) or (enabled != self._enabled)
            if not changed:
                return
            self._callsign = callsign
            self._enabled = enabled and bool(callsign)
            self._generation += 1
            my_generation = self._generation
            self._state = "connecting" if self._enabled else "disabled"

        if self._enabled:
            self._thread = threading.Thread(target=self._loop, args=(my_generation,), daemon=True)
            self._thread.start()

    def configure_backfill(self, enabled: bool, api_key: str) -> None:
        with self._lock:
            self._backfill_enabled = bool(enabled)
            self._api_key = (api_key or "").strip()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "state": self._state,
                "callsign": self._callsign,
                "message_count": len(self._messages),
                "backfill": {
                    "enabled": self._backfill_enabled,
                    "has_key": bool(self._api_key),
                    "callsigns": self._backfill_callsigns_locked() if self._callsign else [],
                    "last": self._backfill_status,
                },
            }

    def messages(self) -> list:
        with self._lock:
            return list(self._messages)

    def gaps(self) -> list:
        with self._lock:
            return list(self._gaps)

    # ------------------------------------------------------------------
    # backfill
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return
        try:
            if st.get("last_alive"):
                self._last_alive = float(st["last_alive"])
            if st.get("pending_since"):
                self._pending_since = float(st["pending_since"])
            for mid in st.get("seen_ids") or []:
                self._seen_ids.append(str(mid))
            if isinstance(st.get("last_status"), dict):
                self._backfill_status = st["last_status"]
        except Exception:
            pass

    def _save_state_locked(self) -> None:
        """Caller holds self._lock. Never raises."""
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "last_alive": self._last_alive,
                    "pending_since": self._pending_since,
                    "seen_ids": list(self._seen_ids),
                    "last_status": self._backfill_status,
                }, f)
            os.replace(tmp, STATE_FILE)
            self._last_persist = time.time()
        except Exception:
            pass

    def _backfill_callsigns_locked(self) -> list:
        """Exact callsigns to ask aprs.fi about: the configured one, the base
        call, then every SSID of that base that messages have arrived on
        (from the saved history), capped at aprs.fi's 10-recipient limit."""
        base = _base_call(self._callsign)
        out = []
        for c in [self._callsign, base] + [m.get("to", "") for m in self._messages]:
            c = _clean_callsign(c)
            if c and _base_call(c) == base and c not in out:
                out.append(c)
        return out[:APRSFI_MAX_DST]

    def _note_connected(self) -> None:
        """Called once a (re)connection is up: turn the time since the last
        heartbeat into a pending gap, then try to catch it up."""
        now = time.time()
        with self._lock:
            if self._last_alive is not None and now - self._last_alive > BACKFILL_MIN_GAP:
                start = self._last_alive
                self._pending_since = start if self._pending_since is None else min(self._pending_since, start)
            self._last_alive = now
            self._save_state_locked()
        self._ensure_heartbeat()
        self._maybe_backfill()

    def _note_disconnected(self) -> None:
        with self._lock:
            if self._state == "connected":
                self._last_alive = time.time()
                self._save_state_locked()

    def _ensure_heartbeat(self) -> None:
        with self._lock:
            if self._heartbeat is not None and self._heartbeat.is_alive():
                return
            self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        while True:
            time.sleep(HEARTBEAT_TICK)
            with self._lock:
                if self._state == "connected":
                    self._last_alive = time.time()
                    if time.time() - self._last_persist >= HEARTBEAT_PERSIST:
                        self._save_state_locked()
            self._maybe_backfill()

    def _maybe_backfill(self) -> None:
        now = time.time()
        with self._lock:
            if (not self._enabled or not self._backfill_enabled or not self._api_key
                    or self._state != "connected" or self._pending_since is None
                    or self._backfill_running
                    or now - self._last_backfill_at < BACKFILL_MIN_INTERVAL):
                return
            self._backfill_running = True
            self._last_backfill_at = now
            since = self._pending_since
            api_key = self._api_key
            base = _base_call(self._callsign)
            callsigns = self._backfill_callsigns_locked()
        try:
            self._run_backfill(since, api_key, base, callsigns)
        finally:
            with self._lock:
                self._backfill_running = False

    def _fetch_aprsfi(self, api_key: str, callsigns: list) -> list:
        params = urllib.parse.urlencode({
            "what": "msg", "dst": ",".join(callsigns),
            "apikey": api_key, "format": "json",
        })
        req = urllib.request.Request(
            f"{APRSFI_URL}?{params}",
            headers={"User-Agent": f"W1ZLA-Hotspot-Dashboard/{config.APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=max(config.APRS_TIMEOUT, 10)) as resp:
            data = json.loads(resp.read())
        if data.get("result") != "ok":
            raise RuntimeError(data.get("description") or "aprs.fi returned result=fail")
        return data.get("entries") or []

    def _run_backfill(self, since: float, api_key: str, base: str, callsigns: list) -> None:
        now = time.time()
        try:
            entries = self._fetch_aprsfi(api_key, callsigns)
        except Exception as e:
            print(f"[aprs_inbox] backfill from aprs.fi failed: {type(e).__name__}: {e}", flush=True)
            with self._lock:
                # the gap stays pending -- the heartbeat retries after the throttle
                self._backfill_status = {"at": now, "found": 0, "error": str(e)[:200], "callsigns": callsigns}
                self._save_state_locked()
            return

        window_start = since - BACKFILL_MIN_GAP
        parsed = []
        for e in entries:
            try:
                t = float(e.get("time") or 0)
            except (TypeError, ValueError):
                continue
            parsed.append((str(e.get("messageid") or ""), t,
                           _clean_callsign(e.get("srccall", "")),
                           _clean_callsign(e.get("dst", "")),
                           e.get("message") or ""))

        # aprs.fi's 10-latest cap reached with every result inside the gap
        # means older messages from the gap may not have come back. Checked
        # per recipient and overall, since the docs don't say which the cap
        # applies to when several recipients are queried at once.
        maybe_incomplete = False
        by_dst = {}
        for p in parsed:
            by_dst.setdefault(p[3], []).append(p[1])
        for times in list(by_dst.values()) + [[p[1] for p in parsed]]:
            if len(times) >= APRSFI_MAX_ENTRIES and min(times) >= window_start:
                maybe_incomplete = True

        added = []
        with self._lock:
            for mid, t, src, dst, text in parsed:
                if not src or not text or t < window_start or t > now + 60:
                    continue
                if _base_call(dst) != base or _ACK_RE.match(text.strip()):
                    continue
                if mid and mid in self._seen_ids:
                    continue
                if mid:
                    self._seen_ids.append(mid)
                dup = any(
                    _clean_callsign(m.get("from", "")) == src and _clean_callsign(m.get("to", "")) == dst
                    and m.get("message_text") == text
                    and abs(float(m.get("received_at") or 0) - t) <= BACKFILL_DEDUPE_WINDOW
                    for m in self._messages
                )
                if dup:
                    continue
                entry = {
                    "from": src, "to": dst, "message_text": text,
                    "received_at": t, "acked": False,
                    "backfilled": True, "caught_up_at": now,
                }
                added.append(entry)
                storage_notifications.log_notification("aprs", t, entry, MAX_MESSAGES)

            if added:
                merged = sorted(list(self._messages) + added,
                                key=lambda m: float(m.get("received_at") or 0), reverse=True)
                self._messages = deque(merged[:MAX_MESSAGES], maxlen=MAX_MESSAGES)
                gap = {
                    "kind": "gap", "at": now,
                    "offline_sec": max(0, now - since),
                    "count": len(added),
                    "maybe_incomplete": maybe_incomplete,
                }
                self._gaps.appendleft(gap)
                storage_notifications.log_notification("aprs_gap", now, gap, MAX_GAPS)

            self._pending_since = None
            self._backfill_status = {"at": now, "found": len(added), "error": None, "callsigns": callsigns}
            self._save_state_locked()
        if added:
            print(f"[aprs_inbox] caught up {len(added)} message(s) from aprs.fi", flush=True)

    def _loop(self, my_generation: int) -> None:
        while True:
            with self._lock:
                if self._generation != my_generation:
                    return  # superseded by a newer configure() call
                callsign = self._callsign
            base = _base_call(callsign)
            try:
                # Unverified login (passcode "-1"), NOT the real computed
                # passcode -- confirmed by live-testing both ways against
                # the real network (see module docstring) that APRS-IS
                # disconnects an existing connection the instant a SECOND
                # one logs in VERIFIED as the same callsign, which is
                # exactly what happens every time aprs_messaging.py (or
                # the ack send below) opens its own short-lived verified
                # connection to send something. An unverified/receive-only
                # login doesn't compete for verified status, so it isn't
                # kicked -- same pattern real APRS-IS monitoring tools
                # (aprs.fi, findu.com) already use. Filtering behavior is
                # identical either way; verified status only gates
                # whether a connection may also inject/transmit, which
                # this listening connection never needs to do.
                ais = aprslib.IS(callsign, passwd="-1", host=APRS_IS_HOST, port=APRS_IS_PORT)
                ais.set_filter(f"b/{base}*")
                ais.connect()
                with self._lock:
                    if self._generation != my_generation:
                        ais.close()
                        return
                    self._state = "connected"
                threading.Thread(target=self._note_connected, daemon=True).start()
                ais.consumer(
                    lambda packet: self._on_packet(packet, base, my_generation),
                    blocking=True, immortal=False, raw=False,
                )
            except Exception:
                pass
            self._note_disconnected()
            with self._lock:
                if self._generation != my_generation:
                    return
                self._state = "reconnecting"
            time.sleep(RECONNECT_BACKOFF)

    def _on_packet(self, packet: dict, base: str, my_generation: int) -> None:
        with self._lock:
            if self._generation != my_generation:
                raise StopIteration  # tells aprslib's consumer() loop to exit; a newer thread has taken over
        if not isinstance(packet, dict):
            return
        if packet.get("format") != "message":
            return
        addresse = _clean_callsign(packet.get("addresse", ""))
        if _base_call(addresse) != base:
            return
        msg_text = packet.get("message_text")
        if msg_text is None:
            # an ack/reject of something we sent, not a message to show
            if packet.get("response") in ("ack", "rej") and packet.get("msgNo"):
                self._on_response(packet.get("from", ""), str(packet["msgNo"]), packet["response"])
            return
        sender = packet.get("from", "")
        msg_no = packet.get("msgNo")

        dedupe_key = (sender, msg_no)
        with self._lock:
            if msg_no and dedupe_key in self._acked:
                return
            if msg_no:
                self._acked.append(dedupe_key)
            entry = {
                "from": sender,
                "to": addresse,  # which specific SSID this was addressed to -- may differ from the base callsign
                "message_text": msg_text,
                "received_at": time.time(),
                "acked": bool(msg_no),
            }
            self._messages.appendleft(entry)
            storage_notifications.log_notification("aprs", entry["received_at"], entry, MAX_MESSAGES)

        if msg_no:
            # Ack FROM the exact SSID the message was addressed to, not the
            # generic configured base callsign -- the sender's client is
            # tracking the conversation with that specific addressee.
            self._send_ack(addresse, sender, msg_no)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    @staticmethod
    def clean_text(text: str) -> str:
        return re.sub(r" {2,}", " ", _BAD_TEXT_CHARS.sub("", text or "")).strip()

    def send(self, to_callsign: str, text: str, via: str = None, contact: str = None) -> tuple:
        """Queue one outbound message from the configured callsign. Returns
        (ok, entry_or_error). The entry is added to the message history
        straight away (outbound=True, status "sending"), then a background
        thread transmits it up to SEND_TRIES times until acked."""
        to_callsign = _clean_callsign(to_callsign)[:9]
        text = self.clean_text(text)[:SEND_MAX_TEXT]
        with self._lock:
            me = self._callsign
            enabled = self._enabled
        if not enabled or not me:
            return False, "Turn on APRS messages in Notifications (and set your callsign) first"
        if not to_callsign or not text:
            return False, "A destination and a message are required"
        now = time.time()
        with self._lock:
            self._msg_counter = (self._msg_counter + 1) % 100000
            msg_no = str(self._msg_counter or 1)
            entry = {
                "outbound": True, "from": me, "to": to_callsign, "message_text": text,
                "received_at": now, "msg_no": msg_no, "tries": 0, "status": "sending",
            }
            if via:
                entry["via"] = via
            if contact:
                entry["contact"] = contact
            self._messages.appendleft(entry)
            storage_notifications.log_notification("aprs", now, entry, MAX_MESSAGES)
        threading.Thread(target=self._send_loop, args=(entry,), daemon=True).start()
        return True, dict(entry)

    def _send_loop(self, entry: dict) -> None:
        for attempt in range(1, SEND_TRIES + 1):
            with self._lock:
                if entry["status"] in ("acked", "rejected"):
                    return
            ok, err = self._transmit(entry["from"], entry["to"], f'{entry["message_text"]}{{{entry["msg_no"]}')
            with self._lock:
                if entry["status"] in ("acked", "rejected"):
                    return
                entry["tries"] = attempt
                entry["status"] = "waiting" if ok else "send_failed"
                if not ok:
                    entry["error"] = err
                storage_notifications.update_notification("aprs", entry["received_at"], entry)
            # wait for the ack, checking often so a quick ack ends the loop early
            deadline = time.time() + SEND_RETRY_SEC
            while time.time() < deadline:
                time.sleep(1)
                with self._lock:
                    if entry["status"] in ("acked", "rejected"):
                        return
        with self._lock:
            if entry["status"] not in ("acked", "rejected"):
                entry["status"] = "no_ack" if entry["status"] == "waiting" else "send_failed"
                storage_notifications.update_notification("aprs", entry["received_at"], entry)

    def _on_response(self, sender: str, msg_no: str, response: str) -> None:
        sender = _clean_callsign(sender)
        with self._lock:
            for m in self._messages:
                if (m.get("outbound") and m.get("msg_no") == msg_no
                        and _clean_callsign(m.get("to", "")) == sender
                        and m.get("status") not in ("acked", "rejected")):
                    m["status"] = "acked" if response == "ack" else "rejected"
                    m["acked_at"] = time.time()
                    storage_notifications.update_notification("aprs", m["received_at"], m)
                    break

    @staticmethod
    def _transmit(my_callsign: str, to_callsign: str, payload: str) -> tuple:
        """One short-lived VERIFIED connection per send, same as _send_ack and
        aprs_messaging.py -- the listener itself is unverified and can't transmit
        (see the module docstring for why they must stay separate)."""
        try:
            passcode = str(aprslib.passcode(my_callsign))
            ais = aprslib.IS(my_callsign, passwd=passcode, host=APRS_IS_HOST, port=APRS_IS_PORT)
            ais.connect()
            ais.sendall(f"{my_callsign}>APRS,TCPIP*::{_addressee_field(to_callsign)}:{payload}")
            ais.close()
            return True, None
        except Exception as e:
            print(f"[aprs_inbox] send to {to_callsign} failed: {type(e).__name__}: {e}", flush=True)
            return False, f"{type(e).__name__}: {e}"[:200]

    @staticmethod
    def _send_ack(my_callsign: str, to_callsign: str, msg_no: str) -> None:
        """Best-effort -- a failed ack just means the sender's client may
        retry the original message a few times, same graceful-degradation
        spirit as every other integration in this app."""
        try:
            passcode = str(aprslib.passcode(my_callsign))
            ais = aprslib.IS(my_callsign, passwd=passcode, host=APRS_IS_HOST, port=APRS_IS_PORT)
            ais.connect()
            packet = f"{my_callsign}>APRS,TCPIP*::{_addressee_field(to_callsign)}:ack{msg_no}"
            ais.sendall(packet)
            ais.close()
        except Exception:
            pass
