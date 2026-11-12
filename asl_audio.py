"""Live receiver meter + RX/TX (COS/PTT) state for ASL3 hotspot cards.

Reads two of the SimpleUSB channel driver's own tune-menu support
displays over the SSH login this app already uses to poll the node --
the same commands ASL's own WebUI (asl-web-ui's tuning.py) and
simpleusb-tune-menu read. Both are display-only: nothing is changed on
the node unless a setting letter or "j" (save) is sent, and this module
never sends input at all.

  susb tune menu-support b   RX voice level bar: one frame per carriage
                             return, "|" + "=" * n + ">" padded to a
                             75-column field + "|". Frames are only sent
                             while the receiver is open (COS) -- confirmed
                             live on W1ZLA/node 59929 (2026-11): 0 frames
                             in 10 s unkeyed, 98 frames in 10 s keyed
                             (~10/s). The bar's own header marks 3 kHz at
                             column 32 and 5 kHz at column 49.
  susb tune menu-support v   Receiver/Transmitter status display: one row
                             per carriage return, "COS in | CTCSS in |
                             COS out | PTT out", each "Keyed"/"Clear"/
                             "Off". Streams continuously, keyed or not
                             (confirmed live: "Clear | Off | Clear |
                             Clear" idle, "Keyed | Off | Keyed | Clear"
                             with a handheld keyed into the node).
                             PTT Out has NOT yet been seen anything but
                             "Clear" live, so anything other than
                             "Clear"/"Off" counts as transmitting.
                             (TX confirmed lighting live 2026-11-12 while
                             a linked node was talking.)
  susb tune menu-support z   Transmit audio statistics: while the node is
                             transmitting, one line per second, e.g.
                             "TxAudioStats: Pk  -7.3  Avg Pwr -19  Min -44
                             Max -14  dBFS  ClipCnt 0" -- peak/average over
                             the last second of audio sent to the radio
                             (res_usbradio.c's ast_radio_print_audio_stats,
                             50 x 20 ms frames). Prints "Tx not keyed" once
                             when the transmitter drops. Confirmed live on
                             node 59929 (2026-11-12). This is what shows
                             link audio, which never passes the receiver.

Replaces the earlier ChanSpy + AudioSocket tap (see CLAUDE.md), which
needed a dialplan provisioned on the node and dropped every ~7-10 s.
SimpleUSB nodes only ("susb"); a USBRadio node uses the "radio" prefix,
which this module falls back to if "susb" isn't a known command --
untested against a real USBRadio node.

Same "N independent persistent connections reconciled against
hotspots.json" shape as openspot.py's OpenSpot4Manager. Never raises out
of reconcile()/snapshot().
"""
import re
import threading
import time

import paramiko

import config
from storage import load_hotspots

# One frame of the "b" display: the "=" run before ">" is the level.
_BAR_RE = re.compile(r"\|(=*)>")
# One row of the "v" display. Header rows ("COS | CTCSS ...", "Input |
# Input ...") are skipped by the value check below.
_STATUS_RE = re.compile(r"^\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*$")
_STATUS_VALUES = {"Keyed", "Clear", "Off", "On"}
# One line of the "z" display.
_TXSTATS_RE = re.compile(
    r"TxAudioStats:\s*Pk\s*(-?[\d.]+)\s+Avg Pwr\s*(-?[\d.]+).*?ClipCnt\s*(\d+)")


def _is_on(value: str) -> bool:
    return value not in ("Clear", "Off")


class _AslAudioWorker:
    """One SSH connection running both displays for one ASL3 hotspot."""

    def __init__(self, hotspot: dict):
        self._ip   = hotspot["ip"]
        self._id   = hotspot["id"]
        self._user = hotspot.get("user")
        self._pass = hotspot.get("pass")
        self._node = hotspot.get("asl_node", "")
        self._prefix = "susb"
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._level_cols = 0
        self._last_bar_at = 0.0
        self._cos = None
        self._ptt = None
        self._last_status_at = 0.0
        self._tx_peak_db = None
        self._tx_avg_db = None
        self._tx_clip = 0
        self._last_tx_at = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def config_matches(self, hotspot: dict) -> bool:
        return (hotspot.get("id") == self._id
                and hotspot.get("ip") == self._ip
                and hotspot.get("user") == self._user
                and hotspot.get("pass") == self._pass
                and hotspot.get("asl_node", "") == self._node)

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            # The status display streams constantly, so its freshness is
            # the connection's heartbeat; the bar is silent while idle.
            connected = (now - self._last_status_at) <= config.ASL_AUDIO_STALE_SEC
            bar_fresh = (now - self._last_bar_at) <= config.ASL_METER_BAR_HOLD_SEC
            cols = self._level_cols if (connected and bar_fresh and self._cos is not False) else 0
            # Transmit audio: one reading per second, so held a little
            # longer than that before it counts as gone.
            tx_fresh = (connected and self._tx_peak_db is not None
                        and (now - self._last_tx_at) <= config.ASL_METER_TX_HOLD_SEC)
            tx_pct = None
            if tx_fresh:
                floor = config.ASL_METER_TX_FLOOR_DB
                tx_pct = round(max(0.0, min(1.0, (self._tx_peak_db - floor) / -floor)) * 100, 1)
            return {
                "connected": connected,
                "level_cols": cols,
                "level_pct": round(cols / config.ASL_METER_BAR_COLS * 100, 1),
                "cos": self._cos if connected else None,
                "ptt": self._ptt if connected else None,
                "tx_pct": tx_pct,
                "tx_peak_db": self._tx_peak_db if tx_fresh else None,
                "tx_avg_db": self._tx_avg_db if tx_fresh else None,
                "tx_clip": self._tx_clip if tx_fresh else None,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:
                print(f"asl_audio: {self._ip} (node {self._node}): {type(e).__name__}: {e}")
            with self._lock:
                self._cos = self._ptt = None
                self._level_cols = 0
                self._tx_peak_db = self._tx_avg_db = None
                # A healthy stream that just ended (the remote `timeout`
                # expiring) restarts at once; a failing one backs off.
                healthy = time.time() - self._last_status_at < 5
            self._stop_event.wait(1 if healthy else config.ASL_AUDIO_RECONNECT_BACKOFF)

    def _cmd(self, which: str) -> str:
        # `timeout` so the remote asterisk -rx can never outlive this app's
        # connection for long; the loop below just restarts it.
        return (f'sudo timeout {config.ASL_METER_CMD_MAX_SEC} '
                f'asterisk -rx "{self._prefix} tune menu-support {which}"')

    def _run_once(self) -> None:
        if not self._node.isdigit():
            raise ValueError(f"no valid ASL node number configured for {self._ip}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self._ip, username=self._user, password=self._pass,
                           timeout=config.SSH_TIMEOUT)
            transport = client.get_transport()
            chans = {}
            for which in ("v", "b", "z"):
                ch = transport.open_session()
                ch.exec_command(self._cmd(which))
                ch.settimeout(0.0)
                chans[which] = ch
            bufs = {"v": "", "b": "", "z": ""}
            started = time.time()
            while not self._stop_event.is_set():
                got = False
                for which, ch in chans.items():
                    if ch.closed:
                        continue
                    try:
                        data = ch.recv(4096)
                    except Exception:
                        data = None  # nothing ready (non-blocking)
                    if data:
                        got = True
                        bufs[which] = self._consume(which, bufs[which] + data.decode("utf-8", "ignore"))
                    while ch.recv_stderr_ready():
                        err = ch.recv_stderr(4096).decode("utf-8", "ignore").strip()
                        if err:
                            print(f"asl_audio: {self._ip} (node {self._node}) stderr: {err}")
                # The status display never goes quiet while healthy.
                if chans["v"].exit_status_ready():
                    raise EOFError("status display ended")
                if time.time() - started > 3 and not self._last_status_at_after(started):
                    raise TimeoutError("no status rows from the node -- is this a SimpleUSB/USBRadio node?")
                if not got:
                    time.sleep(0.05)
        finally:
            client.close()

    def _last_status_at_after(self, t: float) -> bool:
        with self._lock:
            return self._last_status_at >= t

    def _consume(self, which: str, buf: str) -> str:
        if "No such command" in buf:
            # Not a SimpleUSB node: try the USBRadio driver next time.
            self._prefix = "radio" if self._prefix == "susb" else "susb"
            raise RuntimeError(f"'{buf.strip()[:80]}' -- switching to '{self._prefix}' prefix")
        parts = re.split(r"[\r\n]", buf)
        rest = parts.pop()  # incomplete tail, kept for the next read
        if which == "b":
            # A frame may also be complete before its CR arrives.
            m = None
            for seg in parts + [rest]:
                for m in _BAR_RE.finditer(seg):
                    pass
            if m is not None:
                with self._lock:
                    self._level_cols = len(m.group(1))
                    self._last_bar_at = time.time()
        elif which == "z":
            for seg in parts:
                if "Tx not keyed" in seg:
                    with self._lock:
                        self._tx_peak_db = self._tx_avg_db = None
                    continue
                tm = _TXSTATS_RE.search(seg)
                if tm:
                    with self._lock:
                        self._tx_peak_db = float(tm.group(1))
                        self._tx_avg_db = float(tm.group(2))
                        self._tx_clip = int(tm.group(3))
                        self._last_tx_at = time.time()
        else:
            for seg in parts:
                sm = _STATUS_RE.match(seg)
                if not sm or not all(v in _STATUS_VALUES for v in sm.groups()):
                    continue
                cos_in, _ctcss, _cos_out, ptt = sm.groups()
                with self._lock:
                    self._cos = _is_on(cos_in)
                    self._ptt = _is_on(ptt)
                    self._last_status_at = time.time()
        return rest[-400:]


class AslAudioManager:
    """Reconciles hotspots.json's audio_meter_enabled ASL3 entries against
    live per-node workers. Same diff-and-stop-old shape as openspot.py's
    OpenSpot4Manager."""

    def __init__(self):
        self._lock = threading.Lock()
        self._workers: dict[str, _AslAudioWorker] = {}

    def reconcile(self, hotspots: list) -> None:
        # Keyed by hotspot id, not ip -- two ASL3 entries can share an ip.
        desired = {
            h["id"]: h for h in hotspots
            if h.get("type") == "asl3" and h.get("audio_meter_enabled")
               and h.get("enabled", True)
        }
        to_stop = []
        with self._lock:
            for key, worker in list(self._workers.items()):
                hs = desired.get(key)
                if hs is None or not worker.config_matches(hs):
                    to_stop.append(self._workers.pop(key))
            for key, hs in desired.items():
                if key not in self._workers:
                    worker = _AslAudioWorker(hs)
                    worker.start()
                    self._workers[key] = worker
        for worker in to_stop:
            worker.stop()

    def remove(self, hotspot_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(hotspot_id, None)
        if worker is not None:
            worker.stop()

    def snapshot(self) -> dict:
        with self._lock:
            return {key: w.snapshot() for key, w in self._workers.items()}

    def run_forever(self) -> None:
        while True:
            self.reconcile(load_hotspots())
            time.sleep(config.POLL_INTERVAL)
