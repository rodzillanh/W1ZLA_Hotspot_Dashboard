# SPDX-License-Identifier: GPL-3.0-or-later
# Translated from wfweb's cw-decoder-worker.js (GPL-3.0, Alain De Carolis
# K1FM, github.com/adecarolis/wfweb), so this file carries wfweb's licence.
"""DeepCW Morse decoder, run on the dashboard's own server.

A Python port of wfweb's browser decoder worker
(resources/web-shared/cw-decoder-worker.js, github.com/adecarolis/wfweb,
read 2026-11-14), suggested by its author (Alain K1FM) as the way for the
dashboard to show CW copy without wfweb itself decoding anything.

The model (deepcw.onnx, AGPL-3.0, e04/deepcw-engine) and its metadata
(deepcw.onnx.json) are NOT bundled here: they're fetched at startup from the
user's own wfweb (/models/...), the same files its browser decoder loads.

What the worker does, kept exactly:
  - audio at 3200 Hz (rig audio block-averaged down by the rate ratio);
  - a sliding 12 s window, decoded whole on every tick (the model is not a
    streaming decoder -- it labels each frame using context on both sides);
  - spectrogram: reflect pad by fft/2, Hann window, magnitudes of the bins
    covering 400-1200 Hz, log1p, no per-window normalisation;
  - CTC greedy decode, then labels placed on an absolute sample timeline so
    a word is committed once even as the window slides;
  - a label younger than 3 s may still change (pending); a word with no
    space after it is committed anyway at 8 s; the last word of an over is
    committed after 1.5 s with no further labels.
"""

import numpy as np

WINDOW_S = 12
COMMIT_DELAY_S = 3
MAX_HOLD_S = 8
QUIET_TAIL_S = 1.5
MIN_AUDIO_S = 2


def downsample(samples, in_rate, out_rate=3200):
    """Block-average int16/float samples from in_rate down to out_rate.

    in_rate must be an integer multiple of out_rate (48000 -> 3200 is 15).
    Returns float32 in -1..1. Leftover samples that don't fill a block are
    dropped -- callers feeding a stream should carry them over themselves
    (CwStream does).
    """
    ratio = in_rate // out_rate
    x = np.asarray(samples)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    else:
        x = x.astype(np.float32)
    n = (len(x) // ratio) * ratio
    return x[:n].reshape(-1, ratio).mean(axis=1)


class CwDecoder:
    """One decoding stream. feed() takes new 3200 Hz float32 audio and
    returns (committed_text, pending_text) for this tick, the same pair the
    worker posts as segments/pending."""

    def __init__(self, model_bytes, meta, threads=1):
        import onnxruntime as ort
        self.meta = meta
        self.sr = int(meta["sample_rate"])
        self.fft_len = int(meta["fft_length"])
        self.hop = int(meta["hop_length"])
        bin_hz = self.sr / self.fft_len
        self.start_bin = int(np.ceil(meta["spectrogram_min_freq_hz"] / bin_hz))
        self.bins = (int(np.floor(meta["spectrogram_max_freq_hz"] / bin_hz))
                     + 1 - self.start_bin)
        if self.bins != int(meta["spectrogram_frequency_bins"]):
            raise ValueError("metadata expects %s bins, computed %s"
                             % (meta["spectrogram_frequency_bins"], self.bins))
        i = np.arange(self.fft_len)
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * i / self.fft_len)).astype(np.float32)
        self.chars = meta["chars"]
        self.blank = int(meta["blank_index"])
        self.space_idx = self.chars.index(" ")
        self.in_name = meta["onnx_input_name"]
        self.out_name = meta["onnx_output_name"]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(model_bytes, sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self.cap = WINDOW_S * self.sr
        self.ring = np.zeros(0, dtype=np.float32)
        self.ring_base = 0          # absolute sample index of ring[0]
        self.committed_pos = 0      # absolute sample just past the last committed label
        self.committed_open = False  # last commit was a partial word

    def _append(self, chunk):
        ring = np.concatenate([self.ring, chunk]) if len(self.ring) else chunk.copy()
        if len(ring) > self.cap:
            drop = len(ring) - self.cap
            ring = ring[drop:]
            self.ring_base += drop
        self.ring = ring

    def _spectrogram(self, audio):
        pad = self.fft_len >> 1
        padded = np.pad(audio, pad, mode="reflect")
        frames = 1 + (len(padded) - self.fft_len) // self.hop
        if frames <= 0:
            return None
        idx = np.arange(self.fft_len)[None, :] + self.hop * np.arange(frames)[:, None]
        spec = np.fft.rfft(padded[idx] * self.window, axis=1)
        mags = np.abs(spec[:, self.start_bin:self.start_bin + self.bins])
        return np.log1p(mags).astype(np.float32)[None, None, :, :]

    def _decode_frames(self, log_probs):
        best = log_probs[0].argmax(axis=1)
        chars = []
        prev = None
        cur = None
        for f, b in enumerate(best.tolist()):
            if b == self.blank:
                prev = None
                cur = None
                continue
            if b == prev:
                if cur is not None:
                    cur["end"] = f
                continue
            prev = b
            cur = {"ch": self.chars[b] if b < len(self.chars) else "", "start": f, "end": f}
            chars.append(cur)
        return chars

    @staticmethod
    def _chunks(chars):
        out = []
        cur = None
        for c in chars:
            if c["ch"] == " ":
                if cur is not None:
                    cur["closed"] = True
                cur = None
                continue
            if cur is None:
                cur = {"chars": [], "closed": False}
                out.append(cur)
            cur["chars"].append(c)
        return out

    def feed(self, chunk):
        if chunk is not None and len(chunk):
            self._append(np.asarray(chunk, dtype=np.float32))
        ring_len = len(self.ring)
        if ring_len < MIN_AUDIO_S * self.sr:
            return "", ""
        spec = self._spectrogram(self.ring)
        if spec is None:
            return "", ""
        out = self.session.run([self.out_name], {self.in_name: spec})[0]
        chars = self._decode_frames(out)
        for c in chars:
            c["s"] = self.ring_base + c["start"] * self.hop
            c["e"] = self.ring_base + c["end"] * self.hop
        window_end = self.ring_base + ring_len
        commit_before = window_end - COMMIT_DELAY_S * self.sr
        force_before = window_end - MAX_HOLD_S * self.sr
        last_label_end = max((c["e"] for c in chars), default=0)
        slack = 2 * self.hop

        segments = []
        pending = []
        for ck in self._chunks(chars):
            fresh = [c for c in ck["chars"] if c["e"] >= self.committed_pos + slack]
            if not fresh:
                continue
            new_word = fresh[0] is ck["chars"][0]
            last = ck["chars"][-1]
            quiet_after = (last["e"] == last_label_end
                           and window_end - last["e"] >= QUIET_TAIL_S * self.sr)
            complete = ck["closed"] or quiet_after
            if last["e"] < commit_before and complete:
                commit = fresh
            else:
                commit = [c for c in fresh if c["e"] < force_before]
            if commit:
                text = "".join(c["ch"] for c in commit)
                whole = len(commit) == len(fresh) and complete
                segments.append((" " if new_word and self.committed_open else "")
                                + text + (" " if whole else ""))
                self.committed_pos = commit[-1]["e"] + self.hop
                self.committed_open = not whole
            rest = fresh[len(commit):]
            if rest:
                pending.append("".join(c["ch"] for c in rest))
        return "".join(segments), " ".join(pending)
