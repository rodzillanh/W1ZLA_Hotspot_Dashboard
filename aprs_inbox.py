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
"""
import re
import time
import threading
from collections import deque

import aprslib

import storage_notifications

APRS_IS_HOST = "rotate.aprs2.net"
APRS_IS_PORT = 14580
MAX_MESSAGES = 50
RECONNECT_BACKOFF = 15


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
            self._messages.appendleft(payload)

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

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "state": self._state,
                "callsign": self._callsign,
                "message_count": len(self._messages),
            }

    def messages(self) -> list:
        with self._lock:
            return list(self._messages)

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
                ais.consumer(
                    lambda packet: self._on_packet(packet, base, my_generation),
                    blocking=True, immortal=False, raw=False,
                )
            except Exception:
                pass
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
            return  # an ack/reject of something we sent, not a message to show
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
