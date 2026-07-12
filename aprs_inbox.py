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

Filter uses APRS-IS's documented "buddy" filter (`b/CALLSIGN`), which
per the javAPRSFilter spec includes packets addressed to the listed
callsign as well as ones from it -- chosen deliberately over relying on
undocumented "logged-in stations get their own traffic automatically"
assumptions, but worth re-confirming against real inbound traffic once
this is live (see CLAUDE.md).
"""
import re
import time
import threading
from collections import deque

import aprslib

APRS_IS_HOST = "rotate.aprs2.net"
APRS_IS_PORT = 14580
MAX_MESSAGES = 50
RECONNECT_BACKOFF = 15


def _clean_callsign(callsign: str) -> str:
    return re.sub(r"\s+", "", (callsign or "")).upper()


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
            try:
                passcode = str(aprslib.passcode(callsign))
                ais = aprslib.IS(callsign, passwd=passcode, host=APRS_IS_HOST, port=APRS_IS_PORT)
                ais.set_filter(f"b/{callsign}")
                ais.connect()
                with self._lock:
                    if self._generation != my_generation:
                        ais.close()
                        return
                    self._state = "connected"
                ais.consumer(
                    lambda packet: self._on_packet(packet, callsign, my_generation),
                    blocking=True, immortal=False, raw=False,
                )
            except Exception:
                pass
            with self._lock:
                if self._generation != my_generation:
                    return
                self._state = "reconnecting"
            time.sleep(RECONNECT_BACKOFF)

    def _on_packet(self, packet: dict, callsign: str, my_generation: int) -> None:
        with self._lock:
            if self._generation != my_generation:
                raise StopIteration  # tells aprslib's consumer() loop to exit; a newer thread has taken over
        if not isinstance(packet, dict):
            return
        if packet.get("format") != "message":
            return
        addresse = _clean_callsign(packet.get("addresse", ""))
        if addresse != callsign:
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
            self._messages.appendleft({
                "from": sender,
                "message_text": msg_text,
                "received_at": time.time(),
                "acked": bool(msg_no),
            })

        if msg_no:
            self._send_ack(callsign, sender, msg_no)

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
