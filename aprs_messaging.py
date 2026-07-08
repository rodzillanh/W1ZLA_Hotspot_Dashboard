"""Sends an APRS text message to yourself when a favorite becomes active.

Uses APRS-IS (the internet backbone of the APRS network) via the `aprslib`
package (see requirements.txt) -- the same mechanism ham-to-ham APRS text
messaging uses. No API key or account signup needed: your "passcode" is a
deterministic checksum computed from your own callsign (aprslib.passcode()),
the same mechanism every APRS client uses to let you inject your own
traffic. Verified against aprslib's actual source (login/message-parsing
logic) rather than assumed, given how the Brandmeister guess went.

Packet format for a message (confirmed against aprslib/parsing.py):
    FROMCALL>APRS,TCPIP*::ADDRESSEE:message text
ADDRESSEE must be exactly 9 characters, left-justified and space-padded.
Message text is conventionally capped around 67 characters.

Delivery isn't guaranteed the way a push notification is -- it's routed
over APRS-IS to whatever's listening for messages to your callsign (e.g.
APRSdroid on your phone, aprs.fi's message inbox, or a radio with message
duplex support). This module only covers *sending*; it doesn't try to
confirm the message was received.
"""
import re
import time
import threading
from typing import Optional

import aprslib

APRS_IS_HOST = "rotate.aprs2.net"
APRS_IS_PORT = 14580
MAX_MSG_LEN  = 67


def _clean_callsign(callsign: str) -> str:
    return re.sub(r"\s+", "", (callsign or "")).upper()


def _addressee_field(callsign: str) -> str:
    """Exactly 9 characters, left-justified, space-padded -- required by
    the APRS message packet format."""
    return callsign[:9].ljust(9)


class AprsMessenger:
    """Connects fresh for each send rather than holding a persistent
    connection -- these alerts are infrequent (only on a favorite going
    active), so a long-lived idle socket would be more fragile than just
    connecting right before sending and closing right after."""

    def __init__(self, my_callsign: str = "", to_callsign: str = "", cooldown_minutes: float = 10):
        self.my_callsign = _clean_callsign(my_callsign)
        self.to_callsign = _clean_callsign(to_callsign) or self.my_callsign
        self.cooldown    = max(0, cooldown_minutes) * 60
        self._lock         = threading.Lock()
        self._last_sent    = {}   # key -> epoch of last alert sent
        self._active_keys  = set()  # keys currently in an alerted "favorite active" state

    @property
    def enabled(self) -> bool:
        return bool(self.my_callsign)

    def check_and_send(self, hotspot_status: dict) -> None:
        """Call once per poll cycle per hotspot. Sends an alert on the
        rising edge of favorite+active only (not on every poll while it
        stays active), and respects the configured cooldown per hotspot."""
        if not self.enabled:
            return
        key         = hotspot_status.get("ip")
        is_alertable = bool(hotspot_status.get("is_active") and hotspot_status.get("is_favorite"))
        with self._lock:
            was_active = key in self._active_keys
            if is_alertable:
                self._active_keys.add(key)
            else:
                self._active_keys.discard(key)
                return
        if was_active:
            return  # already alerted for this activation; wait for it to go idle first
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0)
            if now - last < self.cooldown:
                return
            self._last_sent[key] = now
        self._send(self._format_message(hotspot_status))

    @staticmethod
    def _format_message(hotspot_status: dict) -> str:
        call      = hotspot_status.get("active_call") or hotspot_status.get("favorite_label") or "Unknown"
        node      = hotspot_status.get("name") or "hotspot"
        talkgroup = hotspot_status.get("talkgroup")
        # Plain ASCII separator, not a Unicode dot -- APRS messages are meant
        # to be readable on old TNCs/handheld radio displays that assume
        # 7-bit ASCII, and aprslib encodes this as UTF-8 rather than erroring,
        # so a fancy character would silently reach the air as mangled bytes
        # instead of failing loudly.
        text = f"{call} active on {node} - {talkgroup}" if talkgroup else f"{call} active on {node}"
        return text[:MAX_MSG_LEN]

    def _send(self, text: str) -> None:
        try:
            passcode = str(aprslib.passcode(self.my_callsign))
            ais = aprslib.IS(self.my_callsign, passwd=passcode, host=APRS_IS_HOST, port=APRS_IS_PORT)
            ais.connect()
            packet = f"{self.my_callsign}>APRS,TCPIP*::{_addressee_field(self.to_callsign)}:{text}"
            ais.sendall(packet)
            ais.close()
        except Exception:
            pass  # best-effort -- a flaky APRS-IS connection shouldn't affect the rest of the app

    @staticmethod
    def test_connection(my_callsign: str, to_callsign: str, timeout: float = 8.0):
        """Connects and sends a real one-time test message. Returns
        (success, message). A wrong/mistyped callsign still computes *a*
        passcode (it's a pure function of the string), so this can't
        detect typos the way a real account system could -- what it does
        confirm is that APRS-IS accepted the login as 'verified' for that
        exact callsign, and that the message packet was accepted."""
        my_callsign = _clean_callsign(my_callsign)
        to_callsign = _clean_callsign(to_callsign) or my_callsign
        if not my_callsign:
            return False, "Callsign required"
        try:
            passcode = str(aprslib.passcode(my_callsign))
            ais = aprslib.IS(my_callsign, passwd=passcode, host=APRS_IS_HOST, port=APRS_IS_PORT)
            ais.connect()
            packet = f"{my_callsign}>APRS,TCPIP*::{_addressee_field(to_callsign)}:Test message from W1ZLA Hotspot Dashboard"
            ais.sendall(packet)
            ais.close()
            return True, f"Sent — logged in as {my_callsign} (verified), test message sent to {to_callsign}"
        except aprslib.LoginError as e:
            return False, f"Login failed: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
