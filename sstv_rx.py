"""SSTV receiver for the SSTV card: streams receive audio from wfweb, finds
SSTV transmissions by their VIS header, decodes them, and keeps the last
few pictures. Receive-only -- this module never transmits and never
touches the rig's frequency (see sstv_controller.py for the idle/tune
logic that decides WHEN this listens).

Everything below was established against a real wfweb (k1fm/wfweb Docker
image, IC-7300 MK2 over LAN) on 2026-09-19, not from its documentation --
which doesn't cover the audio protocol at all:

  - The WebSocket is on wfweb's HTTPS port (same one as the web UI,
    `wss://host:port/`, self-signed cert, no auth) -- the plain-HTTP
    "REST" port does NOT speak WebSocket, despite being labelled "audio"
    in some container templates.
  - Audio is OFF until a client sends `{"cmd":"enableAudio","value":true}`
    (the same command wfweb's own web UI sends); the server answers with
    `{"type":"audioStatus","enabled":true,"sampleRate":48000}`.
  - Audio frames are binary: byte 0 = 0x02 (RX audio), byte 1 = 0, then
    seq (u16 LE), rateDiv (u16 LE), then raw mono signed 16-bit LE PCM
    from byte 6 (wfweb's own UI ignores rateDiv and plays every sample at
    the announced 48 kHz). A 20 ms packet (960 samples) arrives as two
    frames of 682 + 278 samples.
  - Rig state arrives as JSON on the same socket -- `frequency`, `mode`,
    `powerState`, `lanConnected`, and (in `meters` messages)
    `transmitting`. wfweb's own cache doesn't free-run, so like
    wfweb_power.py this sends `{"cmd":"getStatus"}` on an interval.
  - A real, unexplained fault was observed and later disappeared after a
    container restart: for several consecutive test runs exactly one in
    three 20 ms packets arrived MISSING its 682-sample first frame (a
    lone 278 after a 278), silently losing ~14 ms in every 60 ms with no
    sequence-number gap to show for it. SSTV decoded from that audio is
    unusable (horizontal streaks across the whole image), so this module
    (a) zero-fills each detected missing first frame so timing stays
    right, and (b) counts them in `dropped_samples`, which the card's
    "Test audio stream" button reports and every stored image records.

Decoding uses the `sstv` PyPI package (a Rust decoder, imported as
`sstv`) with the mode and start point supplied by sstv_vis.py -- see that
module for why its own automatic header detection isn't used.

Same contract as every other integration module here: self-contained,
never raises out of its public methods, degrades to a status dict with an
`error` string, reconfigured in place via a generation counter (same
shape as wfweb_power.py / hamalert.py). numpy / Pillow / sstv are
imported defensively so an install that hasn't picked up the new
requirements yet shows a clear "not installed" status instead of crashing
app.py at import time.
"""
import io
import json
import os
import ssl
import threading
import time
import uuid

import config

import websocket

# numpy/Pillow/sstv arrived with the SSTV card (v4.91): an install that has
# pulled this code but not yet its new requirements must still START (every
# other card keeps working) and just report why SSTV is unavailable.
try:
    import numpy as np
    from PIL import Image
    import sstv as _sstv
    from sstv_vis import (RATE, IMAGE_START_S, MODE_LABELS, MODE_SECONDS, detect_vis)
    LIBS_ERROR = None
except Exception as _e:  # noqa: BLE001 -- see comment above
    np = Image = _sstv = None
    RATE, IMAGE_START_S, MODE_LABELS, MODE_SECONDS = 48000, 0.910, {}, {}

    def detect_vis(*_a, **_k):
        return []
    LIBS_ERROR = f"SSTV decoder libraries not installed ({_e}) -- rebuild the container / re-run update.sh"

RECV_TIMEOUT = 30
RECONNECT_BACKOFF = 10
STATUS_POLL_INTERVAL = 3.0
ANALYZE_INTERVAL = 1.0
SCAN_INTERVAL = 3.0            # seconds between header scans while searching
SCAN_WINDOW_S = 25             # audio examined per header scan
MIN_HEADER_SCORE = 0.62        # see sstv_vis.py's verification notes
SUPERSEDE_SCORE = 0.85         # a stronger header mid-image replaces a probably-false one
PREVIEW_EVERY = 4.0            # seconds between partial-image previews
FINISH_MARGIN = 3.0            # seconds of audio past the nominal image length
NOISE_STRUCTURE = 40.0         # row-to-row luma difference: real pictures 3.5-10 (synthetic, 28..0 dB),
                               # decoded real rig noise 86 -- calibrated 2026-09
MIN_CONTRAST = 6.0             # luma std-dev below this = a flat/blank decode, not a picture
MAX_GAP_FRACTION = 0.03        # reject an image whose audio lost more than 3% in transit
MAX_RESULT_AGE = 60 * 60 * 24 * 60   # ignore any index entries older than this on load
PAIR_FIRST, PAIR_SECOND = 682, 278   # wfweb's 20 ms packet split, see module docstring

IMAGES_DIRNAME = "sstv"


def _images_dir():
    return os.path.join(config.CONFIG_DIR, IMAGES_DIRNAME)


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _luma(im):
    return np.asarray(im.convert("L"), dtype=np.float32)


def _rows_decoded(im):
    """How many leading rows of a (possibly partial) decode actually hold
    signal -- the decoder leaves not-yet-received rows flat black."""
    a = _luma(im)
    live = np.nonzero(a.std(axis=1) > 0.5)[0]
    return int(live[-1]) + 1 if len(live) else 0


def image_quality(im, rows=None):
    """{"structure", "contrast"} for a decoded image. `structure` is the
    mean absolute luma difference between adjacent rows: real pictures
    are smooth down the page (3.5-10 across the synthetic 28..0 dB test
    range) while a decode of noise is uncorrelated (86 on real rig noise).
    `contrast` is the luma standard deviation -- near zero means a flat
    decode of silence/white noise (structure alone would call that
    'perfectly smooth')."""
    a = _luma(im)
    if rows:
        a = a[:rows]
    if a.shape[0] < 3:
        return None
    return {"structure": float(np.abs(np.diff(a, axis=0)).mean()), "contrast": float(a.std())}


def quality_label(structure):
    if structure < 12:
        return "good"
    if structure < 25:
        return "fair"
    return "weak"


def _freq_shift(x, hz):
    """Shift audio by `hz` (analytic-signal method) -- undoes an SSB
    mistuning sstv_vis measured from the header tones."""
    x = np.asarray(x, dtype=np.float32)
    n = len(x)
    X = np.fft.fft(x)
    h = np.zeros(n, dtype=np.float32)
    h[0] = 1
    h[1:(n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    an = np.fft.ifft(X * h)
    return (an * np.exp(-2j * np.pi * hz * np.arange(n, dtype=np.float32) / RATE)).real


def _decode(samples, mode_name, offset_hz):
    """Decode audio that STARTS at the first image sample. Returns a PIL
    image (with .info['sstv_complete']) or None. Never raises."""
    try:
        x = samples
        if abs(offset_hz) >= 20:
            x = _freq_shift(x, -offset_hz)
        x = np.clip(np.asarray(x, dtype=np.float32), -32768, 32767).astype(np.int16)
        imgs = _sstv.decode(x, RATE, mode=getattr(_sstv.Mode, mode_name), header=False)
        return imgs[0] if imgs else None
    except Exception as e:
        print(f"[sstv] decode failed: {type(e).__name__}: {e}")
        return None


class SstvReceiver:
    def __init__(self):
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._index_lock = threading.Lock()
        # config
        self._enabled = False
        self._host = ""
        self._port = 8080
        self._use_ssl = True
        self._gen = 0
        self._keep = 12
        self._gate = True
        # connection / rig state (all under _lock)
        self._ws = None
        self._connected = False
        self._listening = False
        self._audio_on = False
        self._audio_rate = None
        self._rig = {"freq_hz": None, "mode": None, "power_state": None,
                     "lan_connected": None, "transmitting": False}
        self._last_error = None
        # audio intake (recv thread appends under _lock; analyzer owns _parts)
        self._chunks = []
        self._last_seq = None
        self._last_n = None
        self._last_audio_at = 0.0
        self._frames = 0
        self._dropped = 0
        self._seq_gaps = 0
        # analyzer-owned
        self._parts = []            # [(abs_start_sample, int16 ndarray)]
        self._total = 0             # absolute samples appended so far
        self._scanned_upto = 0
        self._last_scan = 0.0
        self._rx = None             # in-progress reception (analyzer thread only)
        self._pub_rx = None         # public copy of _rx for status(), under _lock
        self._preview_png = None
        self._listening_since = None
        # results (under _index_lock)
        self._images, self._rejected = self._load_index()

    # ---------- config ----------

    def configure(self, enabled, host, port, use_ssl, keep=12, gate=True):
        host = (host or "").strip()
        port = _as_int(port) or 8080
        enabled = bool(enabled) and bool(host)
        keep = max(1, min(60, _as_int(keep) or 12))
        with self._lock:
            self._keep = keep
            self._gate = bool(gate)
            new = (enabled, host, port, bool(use_ssl))
            if new == (self._enabled, self._host, self._port, self._use_ssl):
                return
            self._enabled, self._host, self._port, self._use_ssl = new
            self._gen += 1
            gen = self._gen
            if not enabled:
                self._connected = False
                self._audio_on = False
                self._rig = {"freq_hz": None, "mode": None, "power_state": None,
                             "lan_connected": None, "transmitting": False}
        if enabled:
            threading.Thread(target=self._loop, args=(gen,), daemon=True).start()
            threading.Thread(target=self._analyze_loop, args=(gen,), daemon=True).start()

    def set_listening(self, on):
        """Start/stop pulling audio and scanning for images. Idempotent."""
        on = bool(on) and LIBS_ERROR is None
        with self._lock:
            if on == self._listening:
                return
            self._listening = on
            if on:
                self._listening_since = time.time()
            else:
                self._listening_since = None
        self._send({"cmd": "enableAudio", "value": on})

    # ---------- public reads ----------

    def status(self):
        with self._lock:
            listening = self._listening
            audio_fresh = (time.time() - self._last_audio_at) < 4.0
            rx = dict(self._pub_rx) if self._pub_rx else None
            out = {
                "libs_ok": LIBS_ERROR is None,
                "enabled": self._enabled,
                "connected": self._connected,
                "listening": listening,
                "listening_since": self._listening_since,
                "audio_ok": bool(listening and self._connected and audio_fresh),
                "sample_rate": self._audio_rate,
                "rig": dict(self._rig),
                "receiving": rx,
                "has_preview": self._preview_png is not None and rx is not None,
                "dropped_samples": self._dropped,
                "seq_gaps": self._seq_gaps,
                "error": LIBS_ERROR or self._last_error,
            }
        with self._index_lock:
            imgs = list(self._images)
            out["rejected_total"] = self._rejected
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        out["last"] = imgs[0] if imgs else None
        out["today"] = sum(1 for i in imgs if i.get("ts", 0) >= midnight)
        out["total"] = len(imgs)
        return out

    def images(self):
        with self._index_lock:
            return list(self._images)

    def image_file(self, image_id):
        """Path to a stored PNG, or None. `image_id` is validated (hex
        only) so it can never escape the sstv directory."""
        image_id = str(image_id or "")
        if not image_id or not all(c in "0123456789abcdef" for c in image_id):
            return None
        with self._index_lock:
            if not any(i.get("id") == image_id for i in self._images):
                return None
        path = os.path.join(_images_dir(), image_id + ".png")
        return path if os.path.isfile(path) else None

    def preview_png(self):
        with self._lock:
            return self._preview_png

    def clear_images(self):
        with self._index_lock:
            for i in self._images:
                try:
                    os.remove(os.path.join(_images_dir(), i["id"] + ".png"))
                except OSError:
                    pass
            self._images = []
            self._rejected = 0
            self._save_index_locked()

    # ---------- persistence ----------

    @staticmethod
    def _load_index():
        try:
            with open(os.path.join(_images_dir(), "index.json"), "r", encoding="utf-8") as f:
                d = json.load(f)
            cutoff = time.time() - MAX_RESULT_AGE
            imgs = [i for i in d.get("images", []) if isinstance(i, dict) and i.get("id") and i.get("ts", 0) >= cutoff]
            return imgs, int(d.get("rejected", 0))
        except Exception:
            return [], 0

    def _save_index_locked(self):
        try:
            os.makedirs(_images_dir(), exist_ok=True)
            path = os.path.join(_images_dir(), "index.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"images": self._images, "rejected": self._rejected}, f)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[sstv] could not save index: {e}")

    def _store_image(self, im, meta):
        """Save the PNG + index entry, prune beyond `keep`."""
        os.makedirs(_images_dir(), exist_ok=True)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")
        with open(os.path.join(_images_dir(), meta["id"] + ".png"), "wb") as f:
            f.write(buf.getvalue())
        with self._index_lock:
            self._images.insert(0, meta)
            with self._lock:
                keep = self._keep
            for old in self._images[keep:]:
                try:
                    os.remove(os.path.join(_images_dir(), old["id"] + ".png"))
                except OSError:
                    pass
            self._images = self._images[:keep]
            self._save_index_locked()

    def _count_reject(self):
        with self._index_lock:
            self._rejected += 1
            self._save_index_locked()

    # ---------- websocket ----------

    def _send(self, obj):
        with self._lock:
            ws, connected = self._ws, self._connected
        if not connected or ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(obj))
            return True
        except Exception:
            return False

    def _loop(self, gen):
        while True:
            with self._lock:
                if gen != self._gen:
                    return
                host, port, use_ssl = self._host, self._port, self._use_ssl
            try:
                self._run_once(gen, host, port, use_ssl)
            except Exception as e:
                with self._lock:
                    if gen != self._gen:
                        return
                    self._connected = False
                    self._last_error = str(e)
            with self._lock:
                if gen != self._gen:
                    return
            time.sleep(RECONNECT_BACKOFF)

    def _run_once(self, gen, host, port, use_ssl):
        scheme = "wss" if use_ssl else "ws"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if use_ssl else None
        ws = websocket.create_connection(f"{scheme}://{host}:{port}/", timeout=RECV_TIMEOUT, sslopt=sslopt)
        stop = threading.Event()
        try:
            with self._lock:
                if gen != self._gen:
                    return
                self._ws = ws
                self._connected = True
                self._last_error = None
                self._last_seq = None
                self._last_n = None
                want_audio = self._listening
            if want_audio:
                self._send({"cmd": "enableAudio", "value": True})
            threading.Thread(target=self._status_pinger, args=(gen, stop), daemon=True).start()
            while True:
                with self._lock:
                    if gen != self._gen:
                        return
                raw = ws.recv()
                if raw is None or raw == "":
                    with self._lock:
                        self._last_error = "Connection closed"
                    return
                if isinstance(raw, (bytes, bytearray)):
                    if raw[:1] == b"\x02":
                        self._on_audio(bytes(raw))
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(msg, dict):
                    self._on_message(msg)
        finally:
            stop.set()
            with self._lock:
                if self._ws is ws:
                    self._ws = None
                self._connected = False
                self._audio_on = False
            try:
                ws.close()
            except Exception:
                pass

    def _status_pinger(self, gen, stop):
        while not stop.wait(STATUS_POLL_INTERVAL):
            with self._lock:
                if gen != self._gen:
                    return
            self._send({"cmd": "getStatus"})

    def _on_message(self, msg):
        with self._lock:
            if msg.get("type") == "audioStatus":
                self._audio_on = bool(msg.get("enabled"))
                self._audio_rate = _as_int(msg.get("sampleRate")) or self._audio_rate
            if "audioSampleRate" in msg:
                self._audio_rate = _as_int(msg.get("audioSampleRate")) or self._audio_rate
            if "frequency" in msg:
                self._rig["freq_hz"] = _as_int(msg["frequency"])
            if "mode" in msg:
                self._rig["mode"] = msg["mode"] if isinstance(msg["mode"], str) else None
            if "powerState" in msg:
                self._rig["power_state"] = bool(msg["powerState"])
            if "lanConnected" in msg:
                self._rig["lan_connected"] = bool(msg["lanConnected"])
            if "transmitting" in msg:
                self._rig["transmitting"] = bool(msg["transmitting"])

    def _on_audio(self, b):
        """One binary RX audio frame. See the module docstring for the
        layout and for the missing-first-frame fault handled here."""
        if len(b) < 8:
            return
        seq = b[2] | (b[3] << 8)
        rate_div = b[4] | (b[5] << 8)
        n = (len(b) - 6) // 2
        with self._lock:
            if not self._listening:
                return
            if self._last_seq is not None and ((seq - self._last_seq) & 0xFFFF) != 1:
                self._seq_gaps += 1
            self._last_seq = seq
            if (n == PAIR_SECOND and rate_div == 48 and self._last_n is not None
                    and self._last_n != PAIR_FIRST):
                # a 278-sample tail NOT preceded by its 682-sample head: that head
                # was lost upstream. Keep the timeline intact with silence.
                self._chunks.append(np.zeros(PAIR_FIRST, dtype=np.int16))
                self._dropped += PAIR_FIRST
            self._last_n = n
            self._chunks.append(np.frombuffer(b, dtype="<i2", count=n, offset=6))
            self._frames += 1
            self._last_audio_at = time.time()

    # ---------- analyzer ----------

    def _analyze_loop(self, gen):
        while True:
            time.sleep(ANALYZE_INTERVAL)
            with self._lock:
                if gen != self._gen:
                    return
            if LIBS_ERROR:
                continue
            try:
                self._analyze_once()
            except Exception as e:
                print(f"[sstv] analyzer error: {type(e).__name__}: {e}")
                with self._lock:
                    self._last_error = f"analyzer: {e}"

    def _reset_audio(self):
        self._parts, self._total = [], 0
        self._scanned_upto = 0
        self._rx = None
        with self._lock:
            self._chunks = []
            self._pub_rx = None
            self._preview_png = None

    def _gather(self, abs_start):
        pieces = []
        for a0, arr in self._parts:
            end = a0 + len(arr)
            if end <= abs_start:
                continue
            pieces.append(arr[max(0, abs_start - a0):])
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)

    def _trim(self, keep_s):
        floor = self._total - int(keep_s * RATE)
        self._parts = [(a0, arr) for a0, arr in self._parts if a0 + len(arr) > floor]

    def _analyze_once(self):
        now = time.time()
        with self._lock:
            listening = self._listening
            chunks, self._chunks = self._chunks, []
            rig = dict(self._rig)
            rate = self._audio_rate
            connected = self._connected
        if not listening:
            if self._parts or self._rx is not None:
                self._reset_audio()
            return
        for c in chunks:
            self._parts.append((self._total, c))
            self._total += len(c)
        if rate not in (None, RATE):
            with self._lock:
                self._last_error = f"unsupported wfweb audio sample rate {rate} (need {RATE})"
            return
        if self._rx is not None and not connected:
            self._abort("connection to wfweb lost", count=False)
            return
        if self._rx is None:
            self._trim(SCAN_WINDOW_S + 5)
            if now - self._last_scan >= SCAN_INTERVAL:
                self._last_scan = now
                det = self._scan_for_header(now, rig)
                if det:
                    print(f"[sstv] {det['mode']} header detected (score {det['score']:.2f}, offset {det['offset_hz']:+d} Hz)")
                    self._begin_reception(det, rig)
        else:
            self._advance_reception(now, rig)

    def _scan_for_header(self, now, rig, min_score=MIN_HEADER_SCORE, after_abs=None):
        if not self._parts:
            return None
        have = self._total - self._parts[0][0]
        if have < 3 * RATE:
            return None
        a0 = max(self._parts[0][0], self._total - SCAN_WINDOW_S * RATE)
        seg = self._gather(a0)
        floor = self._scanned_upto if after_abs is None else after_abs
        for t0, ist, code, mode, score, off in sorted(detect_vis(seg, min_score=min_score), key=lambda r: r[0]):
            start_abs = a0 + int(ist * RATE)
            if start_abs <= floor or ist + 0.15 > len(seg) / RATE:
                continue
            return {"mode": mode, "code": code, "start_abs": start_abs, "score": score, "offset_hz": off}
        return None

    def _begin_reception(self, det, rig):
        self._rx = {
            "mode": det["mode"], "start_abs": det["start_abs"], "score": det["score"],
            "offset_hz": det["offset_hz"], "dur": MODE_SECONDS[det["mode"]],
            "started_at": time.time(), "freq_hz": rig.get("freq_hz"),
            "dropped0": self._dropped, "early_done": False, "preview_at": 0.0, "scan_at": time.time(),
        }
        self._scanned_upto = det["start_abs"]
        with self._lock:
            self._preview_png = None
        self._publish_rx(0.0)

    def _publish_rx(self, elapsed):
        rx = self._rx
        with self._lock:
            if rx is None:
                self._pub_rx = None
            else:
                self._pub_rx = {
                    "mode": rx["mode"], "label": MODE_LABELS.get(rx["mode"], rx["mode"]),
                    "elapsed_s": round(min(elapsed, rx["dur"]), 1), "total_s": rx["dur"],
                    "progress": round(min(1.0, elapsed / rx["dur"]), 3),
                    "started_at": rx["started_at"], "freq_hz": rx["freq_hz"],
                }

    def _abort(self, reason, count=True):
        print(f"[sstv] reception dropped: {reason}")
        if count:
            self._count_reject()
        self._rx = None
        with self._lock:
            self._pub_rx = None
            self._preview_png = None

    def _decode_span(self, rx, a0, a1):
        arr = self._gather(a0)
        if a1 is not None:
            arr = arr[:max(0, a1 - a0)]
        return _decode(arr, rx["mode"], rx["offset_hz"])

    def _advance_reception(self, now, rig):
        rx = self._rx
        elapsed = (self._total - rx["start_abs"]) / RATE
        self._publish_rx(elapsed)
        if rig.get("transmitting"):
            return self._abort("rig started transmitting", count=False)
        # early sanity check: a probably-false header is dropped as soon as the
        # first rows are clearly just noise, freeing the receiver for a real one
        if not rx["early_done"] and elapsed >= min(20.0, 0.25 * rx["dur"]):
            rx["early_done"] = True
            im = self._decode_span(rx, rx["start_abs"], None)
            if im is not None:
                rows = _rows_decoded(im)
                q = image_quality(im, rows)
                if q and rows >= 10 and q["structure"] > NOISE_STRUCTURE:
                    return self._abort(f"header at score {rx['score']:.2f} but the first {rows} rows are noise")
        if elapsed >= 5 and now - rx["preview_at"] >= PREVIEW_EVERY and elapsed < rx["dur"]:
            rx["preview_at"] = now
            im = self._decode_span(rx, rx["start_abs"], None)
            if im is not None:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="PNG")
                with self._lock:
                    self._preview_png = buf.getvalue()
        # a much stronger header showing up mid-image means the first one was wrong
        if elapsed >= 20 and now - rx["scan_at"] >= 5:
            rx["scan_at"] = now
            det = self._scan_for_header(now, rig, min_score=SUPERSEDE_SCORE,
                                        after_abs=rx["start_abs"] + 15 * RATE)
            if det:
                print(f"[sstv] a stronger {det['mode']} header (score {det['score']:.2f}) replaces the current reception")
                self._abort("superseded by a stronger header", count=False)
                return self._begin_reception(det, rig)
        if elapsed >= rx["dur"] + FINISH_MARGIN:
            return self._finalize(rx)
        if elapsed > rx["dur"] + 60:
            self._abort("timed out")

    def _finalize(self, rx):
        end_abs = rx["start_abs"] + int((rx["dur"] + FINISH_MARGIN) * RATE)
        im = self._decode_span(rx, rx["start_abs"], min(end_abs, self._total))
        self._scanned_upto = rx["start_abs"] + int(rx["dur"] * RATE)
        self._rx = None
        with self._lock:
            self._pub_rx = None
            self._preview_png = None
            gate = self._gate
        if im is None:
            return self._count_reject()
        q = image_quality(im) or {"structure": 999.0, "contrast": 0.0}
        gaps = self._dropped - rx["dropped0"]
        gap_frac = gaps / max(1, int(rx["dur"] * RATE))
        if gate and gap_frac > MAX_GAP_FRACTION:
            # audio that lost this much in transit decodes to streaks that a
            # smoothness check can still call "fair" -- never show it
            print(f"[sstv] {rx['mode']} image rejected: {gap_frac * 100:.0f}% of its audio was lost in transit")
            return self._count_reject()
        if gate and (q["structure"] > NOISE_STRUCTURE or q["contrast"] < MIN_CONTRAST):
            print(f"[sstv] {rx['mode']} image rejected (structure {q['structure']:.1f}, contrast {q['contrast']:.1f})")
            return self._count_reject()
        meta = {
            "id": uuid.uuid4().hex[:12], "ts": time.time(), "started_at": rx["started_at"],
            "mode": rx["mode"], "label": MODE_LABELS.get(rx["mode"], rx["mode"]),
            "width": im.size[0], "height": im.size[1], "duration_s": rx["dur"],
            "freq_hz": rx["freq_hz"], "quality": quality_label(q["structure"]),
            "structure": round(q["structure"], 1), "contrast": round(q["contrast"], 1),
            "complete": bool(im.info.get("sstv_complete", True)), "offset_hz": rx["offset_hz"],
            "dropped_samples": gaps,
        }
        try:
            self._store_image(im, meta)
            print(f"[sstv] stored {meta['label']} image ({meta['quality']}, structure {meta['structure']})")
        except Exception as e:
            print(f"[sstv] could not store image: {e}")

    # ---------- settings-page test ----------

    @staticmethod
    def test_stream(host, port, use_ssl, seconds=4.0):
        """Settings/drawer 'Test audio stream': a fresh one-off connection
        that enables audio briefly and reports what actually arrived --
        including the missing-first-frame fault described above. Never
        touches the live receiver. Returns (ok, message)."""
        host = (host or "").strip()
        port = _as_int(port) or 8080
        if not host:
            return False, "wfweb host is not set (Settings -> Integrations)"
        scheme = "wss" if use_ssl else "ws"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if use_ssl else None
        try:
            ws = websocket.create_connection(f"{scheme}://{host}:{port}/", timeout=6, sslopt=sslopt)
        except Exception as e:
            return False, f"Could not connect to wfweb: {e}"
        frames = orphans = samples = 0
        last_n = None
        rate = None
        try:
            ws.settimeout(2)
            ws.send(json.dumps({"cmd": "enableAudio", "value": True}))
            end = time.time() + seconds
            while time.time() < end:
                try:
                    m = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if isinstance(m, (bytes, bytearray)):
                    if m[:1] != b"\x02":
                        continue
                    n = (len(m) - 6) // 2
                    if n == PAIR_SECOND and last_n is not None and last_n != PAIR_FIRST:
                        orphans += 1
                    last_n = n
                    frames += 1
                    samples += n
                else:
                    try:
                        j = json.loads(m)
                        if j.get("type") == "audioStatus":
                            rate = _as_int(j.get("sampleRate"))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass
            try:
                ws.send(json.dumps({"cmd": "enableAudio", "value": False}))
            except Exception:
                pass
        except Exception as e:
            return False, f"Stream error: {e}"
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if frames == 0:
            return False, "Connected, but no audio frames arrived"
        khz =f"{(rate or RATE) // 1000} kHz"
        if orphans:
            return False, f"Audio is arriving with gaps: {orphans} of {frames} frames lost their head ({khz}). Restarting the wfweb container cleared this once before."
        return True, f"Stream OK · {khz} · {samples / seconds / 1000:.1f}k samples/s · 0 packets dropped"
