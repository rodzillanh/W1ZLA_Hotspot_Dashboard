"""wfweb's own native WebSocket protocol for listing/writing/renaming/
clearing/recalling a rig's memory channels (v5.5) -- the feature behind
this app's memory-channel tab on the HF Favorites card. Confirmed from
wfweb's own C++ source (github.com/adecarolis/wfweb, webserver.cpp),
NOT documented in wfweb's own REST_API.md, which only covers listing
memories (GET /api/v1/radio/memories, a snapshot of whatever's already
cached -- no live scan) and recalling one (POST .../recall) -- writing,
renaming and clearing a channel exist ONLY over the WebSocket, same
"REST_API.md's own claim that everything is also available over REST is
false for a few commands" situation wfweb_power.py already documented
for setPower/disconnectLan/reconnectLan.

Same wss://<host>:<port>/ endpoint wfweb_power.py's persistent
connection uses -- but every method here opens its OWN short-lived
connection instead of reusing that one. Memory operations are
occasional, explicit user actions (open the tab, save a channel), not
something that benefits from a connection kept warm, and correlating a
specific request to its reply is simpler on a fresh connection than
threading it through the persistent listener's existing
broadcast-only message loop.

Confirmed live (2026-09) against the user's real wfweb + IC-7300MK2:
connecting sends TWO unsolicited messages on connect, not the one
("status") wfweb_power.py's own docstring describes -- a `rigInfo`
(confirming `hasMemoryMode`/`memoriesLocal`, this rig answers
memoriesLocal=False, meaning it stores channels on the radio itself,
not wfweb's own separate list -- exactly the memory-write bug fixed in
0.9.1) immediately followed by a `logbook` message (new in 0.9.1's own
server-side logbook feature, unrelated to memories, just also pushed
on connect). Also confirmed live: the connection intersperses BINARY
websocket frames (spectrum/waterfall data) among the JSON text frames
even with no client-side opt-in requested -- every recv loop below
must check `isinstance(raw, bytes)` and skip binary frames outright,
never hand them to `json.loads()` (a real crash hit while verifying
this: json's own encoding auto-detection misread one such frame as
UTF-32 and raised UnicodeDecodeError instead of something catchable).

Wire protocol, confirmed from source:
  - {"cmd":"getMemories","start":N,"end":N,"group":N} triggers an async
    scan of the RIG's own memory channels (throttled ~500ms/channel on
    wfweb's own side -- a full 1-99 scan can take up to ~50s). The
    server pushes one {"type":"memoryChannel","memory":{...}} per
    NON-EMPTY channel found in range, then a final
    {"type":"memoryScanComplete","count":N} once the range is
    exhausted. A rig that can't report channel *contents* at all
    (memoriesLocal=True) answers instead with wfweb's own separately
    -maintained channel list, same message shapes, no scan/timer
    involved -- every entry for that case carries "local": true.
  - {"cmd":"writeMemory","channel":N,"group":N,"name":"..."} writes
    the rig's CURRENTLY TUNED frequency/mode/tone/duplex to that
    channel -- there's no way to write an arbitrary frequency to a
    channel that isn't the one actively on the VFO; the write always
    captures whatever the rig is tuned to right now. Confirmed
    correct/working from a real earlier session (see
    project_wfweb_memory_write_fix memory) -- not independently
    re-tested in THIS module's own build, deliberately, to avoid
    writing to the user's real radio state without them driving it.
    Echoes back as a broadcast "memoryChannel" (not a direct per-
    request reply) once the rig confirms, or a
    {"type":"error","message":"..."} if refused (no cached frequency
    yet, or a duplex shift set with no offset).
  - {"cmd":"renameMemory","channel":N,"group":N,"name":"..."} rewrites
    an EXISTING channel with a new name, keeping every other field
    from wfweb's own cached copy of it -- refuses (silently, per
    source, unless a client id is attached -- see below) if that
    channel isn't already cached from a prior getMemories scan.
    Echoes a "memoryChannel" broadcast on success.
  - {"cmd":"clearMemory","channel":N,"group":N} deletes a channel --
    echoes a "memoryChannel" broadcast with "del": true.
  - {"cmd":"recallMemory","channel":N,"group":N} selects the channel
    ON THE RADIO itself (so tone/duplex/split actually apply, unlike
    just tuning the VFO to its frequency) -- confirmed from source
    there is NO reply message that specifically confirms this; success
    is inferred here the same way this app infers success for every
    other fire-and-forget tune action (rigctl.py's own tap-to-tune),
    by the absence of a "memoryError" within a short window.
  - {"type":"memoryError","error":"..."} is recall/rename/clear's
    generic failure shape; writeMemory's own failures use the plain
    {"type":"error","message":"..."} shape instead -- confirmed as a
    real (if slightly inconsistent) distinction in the source, not
    assumed to be a typo to paper over.

No auth on this connection (self-signed TLS, verification disabled)
-- same trusted-LAN posture as every other integration in this app,
including wfweb_power.py's own identical connection.
"""
import json
import ssl
import time

import websocket

CONNECT_TIMEOUT = 8.0
RECV_TIMEOUT = 5.0
SCAN_TOTAL_TIMEOUT = 65.0   # a full 1-99 scan at ~500ms/channel, plus headroom
WRITE_TIMEOUT = 8.0
RECALL_SETTLE = 2.5         # how long to wait for a memoryError before assuming success


def _ws_url(host, port, use_ssl):
    scheme = "wss" if use_ssl else "ws"
    return f"{scheme}://{host}:{port}/"


def _connect(host, port, use_ssl, timeout=CONNECT_TIMEOUT):
    sslopt = {"cert_reqs": ssl.CERT_NONE} if use_ssl else None
    return websocket.create_connection(_ws_url(host, port, use_ssl), timeout=timeout, sslopt=sslopt)


def _recv_json(ws, timeout):
    """One recv, skipping binary frames outright and any text frame that
    isn't valid JSON. Returns None on timeout/closed connection."""
    ws.settimeout(timeout)
    raw = ws.recv()
    if not raw:
        return None
    if isinstance(raw, bytes):
        return "__binary__"
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "__binary__"
    return msg if isinstance(msg, dict) else "__binary__"


class WfwebMemoryClient:
    """Stateless -- every call takes host/port/use_ssl explicitly (read
    fresh from settings by the caller) and opens its own connection, no
    configure()/persistent state like wfweb_power.py's class."""

    @staticmethod
    def list_memories(host, port, use_ssl, start=1, end=99, group=0):
        """Runs a full getMemories scan. Returns (memories, error) --
        memories is a list of dicts (only the non-empty channels wfweb
        reports, same as what its own browser UI's memory panel shows),
        error is None on a clean scanComplete."""
        host = (host or "").strip()
        if not host:
            return [], "No wfweb host configured"
        try:
            ws = _connect(host, port, use_ssl)
        except Exception as e:
            return [], f"Could not connect: {e}"
        try:
            ws.send(json.dumps({"cmd": "getMemories", "start": start, "end": end, "group": group}))
            out = []
            deadline = time.time() + SCAN_TOTAL_TIMEOUT
            while time.time() < deadline:
                msg = _recv_json(ws, min(RECV_TIMEOUT, max(0.5, deadline - time.time())))
                if msg is None:
                    return out, "Connection closed mid-scan"
                if msg == "__binary__":
                    continue
                mtype = msg.get("type")
                if mtype == "memoryChannel":
                    mem = msg.get("memory")
                    if isinstance(mem, dict) and not mem.get("del") and not mem.get("empty"):
                        out.append(mem)
                elif mtype == "memoryScanComplete":
                    out.sort(key=lambda m: (m.get("group", 0), m.get("channel", 0)))
                    return out, None
                elif mtype == "memoryError":
                    return out, msg.get("error") or "Memory scan failed"
            return out, "Timed out waiting for the scan to finish"
        except Exception as e:
            return [], f"{type(e).__name__}: {e}"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _send_and_wait_for_channel(host, port, use_ssl, cmd, channel, group, timeout):
        """Shared by write/rename: send `cmd`, wait for the matching
        "memoryChannel" broadcast (or an error) for this exact
        channel/group. Returns (memory_dict|None, error)."""
        host = (host or "").strip()
        if not host:
            return None, "No wfweb host configured"
        try:
            ws = _connect(host, port, use_ssl)
        except Exception as e:
            return None, f"Could not connect: {e}"
        try:
            ws.send(json.dumps(cmd))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = _recv_json(ws, min(RECV_TIMEOUT, max(0.5, deadline - time.time())))
                if msg is None:
                    return None, "Connection closed"
                if msg == "__binary__":
                    continue
                mtype = msg.get("type")
                if mtype == "memoryChannel":
                    mem = msg.get("memory") or {}
                    if mem.get("channel") == channel and mem.get("group", 0) == group:
                        return mem, None
                elif mtype == "memoryError":
                    return None, msg.get("error") or "Memory operation failed"
                elif mtype == "error":
                    return None, msg.get("message") or "Memory operation failed"
            return None, "Timed out waiting for a response"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def write_memory(host, port, use_ssl, channel, name="", group=0):
        """Writes the rig's CURRENTLY TUNED frequency/mode to `channel`
        -- there is no "set frequency X on channel Y" here, only "save
        what's live right now." Tune the rig first (rigctl.py) if a
        specific frequency needs to land in this channel."""
        cmd = {"cmd": "writeMemory", "channel": channel, "group": group, "name": name or ""}
        return WfwebMemoryClient._send_and_wait_for_channel(host, port, use_ssl, cmd, channel, group, WRITE_TIMEOUT)

    @staticmethod
    def rename_memory(host, port, use_ssl, channel, name, group=0):
        cmd = {"cmd": "renameMemory", "channel": channel, "group": group, "name": name or ""}
        return WfwebMemoryClient._send_and_wait_for_channel(host, port, use_ssl, cmd, channel, group, WRITE_TIMEOUT)

    @staticmethod
    def clear_memory(host, port, use_ssl, channel, group=0):
        """Returns (ok, error). A cleared channel echoes back with
        del=true, which _send_and_wait_for_channel's caller-agnostic
        match already accepts (it only checks channel/group, not del),
        so this reuses the same helper rather than a third copy of the
        recv loop."""
        cmd = {"cmd": "clearMemory", "channel": channel, "group": group}
        mem, err = WfwebMemoryClient._send_and_wait_for_channel(host, port, use_ssl, cmd, channel, group, WRITE_TIMEOUT)
        return (mem is not None or err is None), err

    @staticmethod
    def recall_memory(host, port, use_ssl, channel, group=0):
        """Selects `channel` on the radio itself. No message confirms
        success specifically (confirmed from source) -- treated as
        successful if no memoryError arrives within RECALL_SETTLE."""
        host = (host or "").strip()
        if not host:
            return False, "No wfweb host configured"
        try:
            ws = _connect(host, port, use_ssl)
        except Exception as e:
            return False, f"Could not connect: {e}"
        try:
            ws.send(json.dumps({"cmd": "recallMemory", "channel": channel, "group": group}))
            deadline = time.time() + RECALL_SETTLE
            while time.time() < deadline:
                msg = _recv_json(ws, max(0.2, deadline - time.time()))
                if msg is None:
                    break
                if msg == "__binary__":
                    continue
                if msg.get("type") == "memoryError":
                    return False, msg.get("error") or "Recall failed"
            return True, None
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        finally:
            try:
                ws.close()
            except Exception:
                pass
