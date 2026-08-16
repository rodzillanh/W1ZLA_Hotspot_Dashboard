"""Live audio-level metering for ASL3 (AllStarLink) hotspot cards.

Real PCM off a node's own repeater audio, reduced to an RMS level for the
dashboard's VU meter -- ASL3 has no MMDVM-style RSSI/BER telemetry at all
(it's IAX2/Asterisk, not a radio modem), so unlike WPSD there was nothing
to drive that meter with until now. See config.py's ASL_AUDIO_* constants
and CLAUDE.md for the full research/diagnostic session this was built
from (a real, live W1ZLA/node 59929 SSH capture, 2026-08) -- do not touch
the mechanism below without re-verifying against a real node the same way.

Mechanism: ChanSpy() attached to the node's own rxchannel -- the same
persistent, always-"Up" channel `rpt xnode` already treats as the RF
receiver (confirmed live, e.g. "SimpleUSB/59929 ... Repeater Rx") --
whispering that audio into a second channel running AudioSocket(), a
plain TCP client connection carrying raw 16-bit/8kHz mono PCM per
Asterisk's own AudioSocket dialplan app. That TCP connection rides an SSH
REVERSE port forward on the SAME SSH login this app already uses to poll
the node (paramiko's request_port_forward) -- the node connects to its
own 127.0.0.1:ASL_AUDIO_TUNNEL_PORT, and SSH tunnels it back here. No new
inbound port on the dashboard host, no new credential beyond the SSH
login already stored in hotspots.json. See config.py's own comment for
why this was chosen over Asterisk ARI's Snoop+externalMedia (also
confirmed available on the diagnosed build, but RTP/UDP-based, which
can't ride the same tunnel).

This is a fully separate subsystem from monitor.py's poll loop -- audio
level changes far faster than the 5s SSH poll, so it's its own set of
persistent per-node connections, same "N independent workers reconciled
against hotspots.json" shape as openspot.py's OpenSpot4Manager (there's
no clean per-card "someone is looking at this" signal the way
camera_stream.py's real HTTP MJPEG viewers give it, since the meter is
delivered over a polled JSON endpoint, not a stream) -- not something
bolted onto FleetMonitor/HotspotStatus.

Requires the node to have already been provisioned once via
provision-audio-meter.sh (loads chan_audiosocket.so/app_audiosocket.so/
app_chanspy.so and installs a small, per-node, fully-static dialplan
include). A worker whose trigger command fails (not provisioned yet, or
AllowTcpForwarding disabled in the node's sshd_config) just retries with
backoff like every other degrade-gracefully integration in this project
-- it never raises out of reconcile()/snapshot().
"""
import math
import struct
import threading
import time

import paramiko

import config
from storage import load_hotspots

# AudioSocket wire framing (Asterisk's own AudioSocket dialplan-app
# docs): a 1-byte "kind" + big-endian 2-byte length header, then that
# many bytes of payload. KIND_AUDIO carries raw 16-bit/8kHz mono PCM
# (signed, little-endian); KIND_HANGUP/KIND_ID/KIND_ERROR carry no audio
# and are just observed/ignored here -- this worker only needs the
# level, not call identity/lifecycle bookkeeping.
_KIND_HANGUP = 0x00
_KIND_ID     = 0x01
_KIND_AUDIO  = 0x10
_KIND_ERROR  = 0xFF


def _recv_exact(chan, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = chan.recv(n - len(buf))
        if not chunk:
            raise EOFError("AudioSocket connection closed mid-frame")
        buf += chunk
    return buf


def _rms_dbfs(payload: bytes) -> float:
    """RMS level in dBFS (0 = digital full-scale, negative below that)
    over a chunk of signed 16-bit LE PCM. A plain struct-based loop, not
    a new dependency -- Python 3.12 dropped stdlib audioop, but this is
    genuinely simple math (~5 lines), not a reverse-engineered protocol
    worth a real dependency for (contrast camera_stream.py's
    bambulabs_api, a real proprietary protocol).

    Deliberately dBFS, not a raw linear 0.0-1.0 fraction of full-scale --
    an earlier version returned `rms / 32768.0` directly, which reads as
    "quiet" for almost all real speech (normal speech RMS typically sits
    tens of dB below full-scale, so a linear fraction against literal
    digital full-scale needs near-clipping-hot audio to show anything on
    a 0-100% bar at all -- reported directly: "it seems to take quite a
    bit of signal to light up the meter"). A logarithmic (dB) value is
    what every real VU-style meter actually uses, for the same reason
    the RSSI meter maps a dBm RANGE rather than treating -60dBm as
    "60% of the way to 0". The actual bar-percentage mapping happens
    client-side (dashboard.html's audioLevelBarPct()), same "send the
    real unit, format for display in JS" split as rssiBarPct()."""
    n = len(payload) // 2
    if n == 0:
        return config.ASL_AUDIO_SILENCE_DBFS
    samples = struct.unpack(f"<{n}h", payload[: n * 2])
    mean_sq = sum(s * s for s in samples) / n
    if mean_sq <= 0:
        return config.ASL_AUDIO_SILENCE_DBFS
    rms = mean_sq ** 0.5
    return max(config.ASL_AUDIO_SILENCE_DBFS, 20.0 * math.log10(rms / 32768.0))


class _AslAudioWorker:
    """One persistent SSH connection + reverse port forward + AudioSocket
    tap for one ASL3 hotspot. Reconnect-with-backoff shape mirrors
    openspot.py's _OpenSpot4Worker -- a fresh SSH login, a fresh reverse
    tunnel, and a fresh `channel originate` trigger every time the tap
    needs (re)establishing, since none of it survives a dropped
    connection."""

    def __init__(self, hotspot: dict):
        self._ip   = hotspot["ip"]
        self._user = hotspot.get("user")
        self._pass = hotspot.get("pass")
        self._node = hotspot.get("asl_node", "")
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._connected = False
        self._level_dbfs = config.ASL_AUDIO_SILENCE_DBFS
        self._last_frame_at = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def config_matches(self, hotspot: dict) -> bool:
        return (hotspot.get("ip") == self._ip
                and hotspot.get("user") == self._user
                and hotspot.get("pass") == self._pass
                and hotspot.get("asl_node", "") == self._node)

    def snapshot(self) -> dict:
        with self._lock:
            stale = (time.time() - self._last_frame_at) > config.ASL_AUDIO_STALE_SEC
            connected = self._connected and not stale
            level = self._level_dbfs if connected else config.ASL_AUDIO_SILENCE_DBFS
            return {"level_dbfs": level, "connected": connected}

    def _set_level(self, level_dbfs: float) -> None:
        with self._lock:
            self._level_dbfs = level_dbfs
            self._last_frame_at = time.time()
            self._connected = True

    def _set_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._level_dbfs = config.ASL_AUDIO_SILENCE_DBFS

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:
                # Never silently swallow -- a bare `except: pass` here
                # made a real failure completely undiagnosable from
                # `docker logs` (see CLAUDE.md's aslstats.py rate-limit
                # gotcha for the same lesson learned once already in this
                # project). Still keeps the "never raises out of a
                # worker thread" contract -- this just makes the failure
                # visible instead of invisible.
                print(f"asl_audio: {self._ip} (node {self._node}): {type(e).__name__}: {e}")
            # A short-lived-but-real connection (audio was actually
            # flowing before something dropped it) gets a much shorter
            # retry delay than a connection that never established at
            # all -- confirmed live (2026-08) that ChanSpy on an
            # app_rpt-managed channel reliably drops after ~7-10s for a
            # reason not fully root-caused (ruled out: Local channel
            # optimization, fixed separately with /n but didn't solve
            # this). Since the tap genuinely works while connected, a
            # fast reconnect keeps the meter's visible gap small instead
            # of sitting disconnected for the full backoff -- but a
            # connection that NEVER establishes (bad credentials, node
            # unreachable, not provisioned) still gets the longer delay,
            # so a genuinely broken node doesn't get hammered with rapid
            # retries indefinitely.
            with self._lock:
                had_connection = self._connected
            self._set_disconnected()
            if not self._stop_event.is_set():
                backoff = (config.ASL_AUDIO_RECONNECT_BACKOFF_AFTER_DROP if had_connection
                           else config.ASL_AUDIO_RECONNECT_BACKOFF)
                self._stop_event.wait(backoff)

    def _request_port_forward_with_retry(self, transport) -> None:
        """A reconnect that follows closely on the PREVIOUS attempt's
        listener teardown can lose a real, confirmed OS-level race -- the
        just-closed port isn't always immediately rebindable (confirmed
        live with the stock `ssh -R` client, not just paramiko -- see
        config.ASL_AUDIO_FORWARD_RETRY_* comment). Retry a few times with
        a short pause before giving up, rather than treating one denial
        as a hard failure needing a full external reconnect+backoff."""
        last_exc = None
        for attempt in range(config.ASL_AUDIO_FORWARD_RETRY_ATTEMPTS):
            try:
                transport.request_port_forward("127.0.0.1", config.ASL_AUDIO_TUNNEL_PORT)
                return
            except paramiko.SSHException as e:
                last_exc = e
                if attempt < config.ASL_AUDIO_FORWARD_RETRY_ATTEMPTS - 1:
                    time.sleep(config.ASL_AUDIO_FORWARD_RETRY_DELAY_SEC)
        raise last_exc

    def _hangup_stale_spy_channels(self, client) -> None:
        """Best-effort cleanup: hang up any of THIS node's own spy-context
        Local channels still lingering when a connection attempt ends,
        for ANY reason (error, timeout, stop). Confirmed necessary live,
        not hypothetical -- a broken version of this feature (a
        fabricated ChanSpy option that never worked, see CLAUDE.md) left
        ~70 orphaned ChanSpy channels running forever on a real
        production repeater, since nothing else was ever going to hang
        them up. Scoped to this node's own context prefix only -- the
        exact same targeted grep+hangup pattern used to manually clean
        those up, never a blanket "channel request hangup all" (which
        would drop real traffic on other links/calls). Swallows its own
        failures -- this is cleanup, not the main flow, and must never
        turn a connection teardown into a new exception."""
        ctx = config.ASL_AUDIO_SPY_CONTEXT_FMT.format(node=self._node)
        cmd = (
            f'sudo asterisk -rx "core show channels concise" | '
            f"grep '^Local/{config.ASL_AUDIO_SPY_EXTEN}@{ctx}' | "
            f"cut -d'!' -f1 | "
            f'while read -r ch; do sudo asterisk -rx "channel request hangup $ch"; done'
        )
        try:
            _, stdout, _ = client.exec_command(cmd, timeout=config.SSH_TIMEOUT)
            stdout.read()
        except Exception:
            pass

    def _run_once(self) -> None:
        if not self._node.isdigit():
            raise ValueError(f"no valid ASL node number configured for {self._ip}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self._ip, username=self._user, password=self._pass,
                            timeout=config.SSH_TIMEOUT)
            transport = client.get_transport()
            self._request_port_forward_with_retry(transport)
            try:
                # Reading stdout (not just discarding exec_command()'s
                # return value) matters here, not just for the diagnostic
                # text: an unreferenced ChannelFile/Channel can be
                # garbage-collected almost immediately, which risks
                # tearing down the SSH channel -- and killing the remote
                # `asterisk -rx "channel originate ..."` -- before it
                # actually finishes triggering the spy call. .read()
                # blocks until the remote command completes and the
                # channel closes normally, same reason monitor.py's own
                # _ssh_exec() always reads stdout rather than firing and
                # walking away.
                _, stdout, stderr = client.exec_command(
                    config.build_asl_audio_originate_cmd(self._node), timeout=config.SSH_TIMEOUT
                )
                originate_out = stdout.read().decode("utf-8", errors="ignore").strip()
                originate_err = stderr.read().decode("utf-8", errors="ignore").strip()
                chan = transport.accept(timeout=config.SSH_TIMEOUT)
                if chan is None:
                    detail = originate_out or originate_err or "(no output)"
                    raise TimeoutError(
                        "AudioSocket never connected back through the tunnel -- "
                        f"channel originate said: {detail!r}. Has "
                        "provision-audio-meter.sh been run against this node?"
                    )
                chan.settimeout(config.ASL_AUDIO_STALE_SEC * 2)
                try:
                    self._read_audiosocket(chan)
                finally:
                    chan.close()
            finally:
                transport.cancel_port_forward("127.0.0.1", config.ASL_AUDIO_TUNNEL_PORT)
        finally:
            self._hangup_stale_spy_channels(client)
            client.close()

    def _read_audiosocket(self, chan) -> None:
        while not self._stop_event.is_set():
            header = _recv_exact(chan, 3)
            kind = header[0]
            length = (header[1] << 8) | header[2]
            payload = _recv_exact(chan, length) if length else b""
            if kind == _KIND_AUDIO:
                self._set_level(_rms_dbfs(payload))
            elif kind in (_KIND_HANGUP, _KIND_ERROR):
                return


class AslAudioManager:
    """Reconciles hotspots.json's audio_meter_enabled ASL3 entries against
    live per-node worker threads. Same diff-and-stop-old shape as
    openspot.py's OpenSpot4Manager -- see that class's own docstring for
    why this "N independent persistent connections" shape fits better
    here than camera_stream.py's viewer-count-gated lazy start."""

    def __init__(self):
        self._lock = threading.Lock()
        self._workers: dict[str, _AslAudioWorker] = {}

    def reconcile(self, hotspots: list) -> None:
        desired = {
            h["ip"]: h for h in hotspots
            if h.get("type") == "asl3" and h.get("audio_meter_enabled")
               and h.get("enabled", True)
        }
        to_stop = []
        with self._lock:
            for ip, worker in list(self._workers.items()):
                hs = desired.get(ip)
                if hs is None or not worker.config_matches(hs):
                    to_stop.append(self._workers.pop(ip))
            for ip, hs in desired.items():
                if ip not in self._workers:
                    worker = _AslAudioWorker(hs)
                    worker.start()
                    self._workers[ip] = worker
        for worker in to_stop:  # outside the lock -- mirrors OpenSpot4Manager/CameraStreamManager
            worker.stop()

    def remove(self, ip: str) -> None:
        with self._lock:
            worker = self._workers.pop(ip, None)
        if worker is not None:
            worker.stop()

    def snapshot(self) -> dict:
        with self._lock:
            return {ip: w.snapshot() for ip, w in self._workers.items()}

    def run_forever(self) -> None:
        """Periodic safety-net reconcile, same rationale as
        OpenSpot4Manager.run_forever() -- every route that mutates
        hotspots.json already calls reconcile() explicitly, so this
        mostly no-ops in practice."""
        while True:
            self.reconcile(load_hotspots())
            time.sleep(config.POLL_INTERVAL)
