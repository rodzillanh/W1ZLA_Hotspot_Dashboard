"""CW Copy card: decodes Morse from wfweb's RX audio on THIS server and keeps
a running transcript, so every open page (and one opened later) sees the
same copy.

wfweb itself decodes nothing server-side -- its CW decoder runs in the
browser -- and its author (Alain K1FM, 2026-11) suggested this approach:
the dashboard takes the same audio frames and runs the same decoder
itself. cw_decoder.py is the decoder (a port of wfweb's worker, verified
tick for tick against it); this module is the plumbing around it:

  - its own wfweb WebSocket (same endpoint and frame format as sstv_rx.py,
    same "one connection per feature" shape as wfweb_packet.py), polling
    getStatus for the rig's frequency and mode;
  - audio is switched on ONLY while the rig reports a CW mode, so nothing
    is streamed or decoded while you're on SSB/FT8;
  - lost halves of wfweb's 682/278 packet split are filled with silence
    (sstv_rx._missing_before) -- a real recurring wfweb fault that turns
    every dash into dits if left unrepaired;
  - the DeepCW model (AGPL-3.0) is downloaded from the user's own wfweb
    (/models/deepcw.onnx + .json) and cached under CONFIG_DIR/cw, revalidated
    with its ETag -- never bundled with this project;
  - committed words are grouped into lines (a new line after a pause, a
    frequency change or ~90 characters) and kept in CONFIG_DIR/cw/
    transcript.json so a restart doesn't lose the recent copy.

Never raises out of its public methods, same contract as every other
integration client here.
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import websocket

import config
import cw_decoder
from sstv_rx import _missing_before

RECV_TIMEOUT = 30
RECONNECT_BACKOFF = 10
STATUS_POLL_INTERVAL = 3       # s between getStatus requests (wfweb doesn't free-run them)
DECODE_TICK_S = 1.0            # the worker is fed one second at a time, same as wfweb's page
LINE_GAP_S = 6                 # a pause this long starts a new transcript line
LINE_MAX_CHARS = 90
LINE_FREQ_JUMP_HZ = 500        # retuning this far starts a new line
MAX_LINES = 200
LOSS_WINDOW_S = 30             # audio-loss percentage is measured over this long
MODEL_FILES = ("deepcw.onnx.json", "deepcw.onnx")


def _cw_dir():
    return os.path.join(config.CONFIG_DIR, "cw")


def is_cw_mode(mode):
    return isinstance(mode, str) and mode.strip().upper().startswith("CW")


class CwCopyService:
    def __init__(self):
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._enabled = False
        self._host = ""
        self._port = 8080
        self._use_ssl = True
        self._gen = 0
        # connection / rig
        self._ws = None
        self._connected = False
        self._audio_on = False
        self._audio_rate = None
        self._rig = {"freq_hz": None, "mode": None}
        self._last_error = None
        # model
        self._decoder = None
        self._model_state = "idle"     # idle | loading | ready | error
        self._model_error = None
        # audio intake
        self._chunks = []
        self._last_n = None
        self._last_audio_at = 0.0
        self._loss = []                # [(t, received, lost)] per tick, last LOSS_WINDOW_S
        # transcript
        self._lines = []               # [{"t", "freq_hz", "text"}], oldest first
        self._pending = ""
        self._last_commit_at = 0.0
        self._load_transcript()

    # ---------- config ----------

    def configure(self, enabled, host, port, use_ssl):
        host = (host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8080
        enabled = bool(enabled) and bool(host)
        with self._lock:
            new = (enabled, host, port, bool(use_ssl))
            if new == (self._enabled, self._host, self._port, self._use_ssl):
                return
            self._enabled, self._host, self._port, self._use_ssl = new
            self._gen += 1
            gen = self._gen
            self._connected = False
            self._audio_on = False
            self._rig = {"freq_hz": None, "mode": None}
            self._decoder = None
            self._model_state = "idle"
            self._model_error = None
        if enabled:
            threading.Thread(target=self._load_model, args=(gen,), daemon=True).start()
            threading.Thread(target=self._loop, args=(gen,), daemon=True).start()
            threading.Thread(target=self._decode_loop, args=(gen,), daemon=True).start()

    # ---------- public reads ----------

    def status(self):
        now = time.time()
        with self._lock:
            mode = self._rig.get("mode")
            in_cw = is_cw_mode(mode)
            recv = sum(r for _, r, _ in self._loss)
            lost = sum(l for _, _, l in self._loss)
            audio_ok = self._audio_on and (now - self._last_audio_at) < 5
            if not self._enabled:
                state = "off"
            elif self._model_state == "error":
                state = "error"
            elif not self._connected:
                state = "connecting"
            elif self._model_state != "ready":
                state = "loading"
            elif not in_cw:
                state = "idle"
            else:
                state = "decoding" if audio_ok else "starting"
            return {
                "enabled": self._enabled,
                "state": state,
                "connected": self._connected,
                "error": self._model_error if self._model_state == "error" else self._last_error,
                "freq_hz": self._rig.get("freq_hz"),
                "mode": mode,
                "in_cw": in_cw,
                "audio_loss_pct": round(lost / (recv + lost) * 100, 1) if (recv + lost) else None,
                "lines": [{"t": l.get("t"), "freq_hz": l.get("freq_hz"), "text": l.get("text", "")}
                          for l in self._lines[-60:]],
                "pending": self._pending if in_cw else "",
            }

    def clear(self):
        with self._lock:
            self._lines = []
            self._pending = ""
            if self._decoder is not None:
                self._decoder.reset()
        self._save_transcript()

    # ---------- model ----------

    def _load_model(self, gen):
        with self._lock:
            self._model_state = "loading"
            host, port, use_ssl = self._host, self._port, self._use_ssl
        try:
            import onnxruntime  # noqa: F401  (checked up front for a clear message)
        except Exception:
            self._model_failed(gen, "The CW decoder needs the onnxruntime package, which "
                                    "only exists for 64-bit systems (x86-64 / arm64).")
            return
        try:
            files = {}
            for name in MODEL_FILES:
                files[name] = self._fetch_cached(host, port, use_ssl, name)
            meta = json.loads(files["deepcw.onnx.json"].decode("utf-8"))
            dec = cw_decoder.CwDecoder(files["deepcw.onnx"], meta, threads=1)
        except Exception as e:
            print(f"[cw] model load failed: {type(e).__name__}: {e}")
            self._model_failed(gen, f"Could not load the DeepCW model from wfweb: {e}")
            return
        with self._lock:
            if gen != self._gen:
                return
            self._decoder = dec
            self._model_state = "ready"
            self._model_error = None

    def _model_failed(self, gen, msg):
        with self._lock:
            if gen == self._gen:
                self._model_state = "error"
                self._model_error = msg

    @staticmethod
    def _fetch_cached(host, port, use_ssl, name):
        """GET /models/<name> from wfweb with If-None-Match against the cached
        copy's ETag; fall back to the cached copy if wfweb can't be reached."""
        d = _cw_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        etag_path = path + ".etag"
        cached = None
        etag = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                cached = f.read()
            if os.path.exists(etag_path):
                with open(etag_path, encoding="utf-8") as f:
                    etag = f.read().strip() or None
        url = f"{'https' if use_ssl else 'http'}://{host}:{port}/models/{name}"
        req = urllib.request.Request(url, headers={"User-Agent": "hotspot-dashboard"})
        if cached is not None and etag:
            req.add_header("If-None-Match", etag)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                body = r.read()
                new_tag = r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached is not None:
                return cached
            if cached is not None:
                return cached
            raise
        except Exception:
            if cached is not None:
                return cached
            raise
        with open(path + ".tmp", "wb") as f:
            f.write(body)
        os.replace(path + ".tmp", path)
        if new_tag:
            with open(etag_path, "w", encoding="utf-8") as f:
                f.write(new_tag)
        return body

    # ---------- connection ----------

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
                    self._last_error = str(e)
            with self._lock:
                if gen != self._gen:
                    return
                self._connected = False
                self._audio_on = False
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
                self._last_n = None
                self._audio_on = False
            self._send({"cmd": "getStatus"})
            threading.Thread(target=self._pinger, args=(gen, stop), daemon=True).start()
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

    def _pinger(self, gen, stop):
        while not stop.wait(STATUS_POLL_INTERVAL):
            with self._lock:
                if gen != self._gen:
                    return
            self._send({"cmd": "getStatus"})
            self._sync_audio()

    def _sync_audio(self):
        """Audio on exactly while the rig is in CW and the model is ready."""
        with self._lock:
            want = is_cw_mode(self._rig.get("mode")) and self._model_state == "ready"
            have = self._audio_on
        if want != have:
            if self._send({"cmd": "enableAudio", "value": want}):
                with self._lock:
                    self._audio_on = want
                    self._last_n = None
                    if not want:
                        self._chunks = []
                        self._pending = ""

    def _on_message(self, msg):
        changed = False
        with self._lock:
            if msg.get("type") == "audioStatus":
                try:
                    self._audio_rate = int(msg.get("sampleRate")) or self._audio_rate
                except (TypeError, ValueError):
                    pass
            if "frequency" in msg:
                try:
                    self._rig["freq_hz"] = int(msg["frequency"])
                except (TypeError, ValueError):
                    pass
            if "mode" in msg and isinstance(msg["mode"], str):
                changed = is_cw_mode(msg["mode"]) != is_cw_mode(self._rig.get("mode"))
                self._rig["mode"] = msg["mode"]
        if changed:
            self._sync_audio()

    def _on_audio(self, b):
        if len(b) < 8:
            return
        rate_div = b[4] | (b[5] << 8)
        n = (len(b) - 6) // 2
        with self._lock:
            if not self._audio_on:
                return
            lost = _missing_before(n, rate_div, self._last_n)
            if lost:
                self._chunks.append(("lost", lost))
            self._last_n = n
            self._chunks.append(("pcm", np.frombuffer(b, dtype="<i2", count=n, offset=6)))
            self._last_audio_at = time.time()

    # ---------- decoding ----------

    def _decode_loop(self, gen):
        carry = np.zeros(0, dtype=np.float32)    # 48 kHz samples not yet averaged down
        buf = np.zeros(0, dtype=np.float32)      # 3200 Hz samples not yet fed
        while True:
            time.sleep(DECODE_TICK_S / 2)
            with self._lock:
                if gen != self._gen:
                    return
                chunks, self._chunks = self._chunks, []
                dec = self._decoder
                rate = self._audio_rate or 48000
                in_cw = self._audio_on
                freq = self._rig.get("freq_hz")
            if dec is None:
                continue
            if not in_cw:
                if len(carry) or len(buf):
                    carry = carry[:0]
                    buf = buf[:0]
                    dec.reset()
                continue
            got = lost = 0
            parts = [carry]
            for kind, v in chunks:
                if kind == "lost":
                    parts.append(np.zeros(v, dtype=np.float32))
                    lost += v
                else:
                    parts.append(v.astype(np.float32) / 32768.0)
                    got += len(v)
            if got or lost:
                now = time.time()
                with self._lock:
                    self._loss.append((now, got, lost))
                    self._loss = [x for x in self._loss if now - x[0] <= LOSS_WINDOW_S]
            x = np.concatenate(parts) if len(parts) > 1 else carry
            ratio = max(1, rate // dec.sr)
            use = (len(x) // ratio) * ratio
            carry = x[use:]
            if use:
                buf = np.concatenate([buf, x[:use].reshape(-1, ratio).mean(axis=1)])
            while len(buf) >= dec.sr * DECODE_TICK_S:
                step = int(dec.sr * DECODE_TICK_S)
                chunk, buf = buf[:step], buf[step:]
                try:
                    committed, pending = dec.feed(chunk)
                except Exception as e:
                    print(f"[cw] decode error: {type(e).__name__}: {e}")
                    with self._lock:
                        self._last_error = f"decoder: {e}"
                    continue
                self._on_result(committed, pending, freq)

    def _on_result(self, committed, pending, freq):
        now = time.time()
        save = False
        with self._lock:
            self._pending = " ".join(pending.split())
            if committed.strip():
                cur = self._lines[-1] if self._lines else None
                new_line = (
                    cur is None
                    or now - self._last_commit_at > LINE_GAP_S
                    or (freq and cur.get("freq_hz") and abs(freq - cur["freq_hz"]) > LINE_FREQ_JUMP_HZ)
                    or len(cur.get("text", "")) + len(committed) > LINE_MAX_CHARS
                )
                # The decoder's own spacing is kept: a finished word ends in a
                # space, a word committed in two parts has none between them.
                if new_line:
                    self._lines.append({"t": now, "freq_hz": freq, "raw": committed.lstrip()})
                    if len(self._lines) > MAX_LINES:
                        self._lines = self._lines[-MAX_LINES:]
                else:
                    cur["raw"] = cur.get("raw", cur.get("text", "") + " ") + committed
                line = self._lines[-1]
                line["text"] = " ".join(line["raw"].split())
                self._last_commit_at = now
                save = True
        if save:
            self._save_transcript()

    # ---------- transcript file ----------

    def _load_transcript(self):
        try:
            with open(os.path.join(_cw_dir(), "transcript.json"), encoding="utf-8") as f:
                d = json.load(f)
            lines = d.get("lines") if isinstance(d, dict) else None
            if isinstance(lines, list):
                self._lines = [l for l in lines if isinstance(l, dict) and "text" in l][-MAX_LINES:]
        except Exception:
            pass

    def _save_transcript(self):
        try:
            os.makedirs(_cw_dir(), exist_ok=True)
            with self._lock:
                data = {"lines": list(self._lines)}
            path = os.path.join(_cw_dir(), "transcript.json")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except Exception:
            pass
