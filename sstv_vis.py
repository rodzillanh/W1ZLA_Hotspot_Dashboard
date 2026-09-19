"""Noise-tolerant SSTV VIS header detector (numpy only) for the SSTV card.

Why this exists instead of using the `sstv` decoder package's own automatic
header detection: that detector was measured (2026-09, against synthetic
Martin M1 mixed into real recorded rig noise) to need roughly 28 dB of
signal-to-noise to find a header at all -- far too strict for HF -- while
the image decoder behind it, given the mode and start point directly
(`header=False`), still produced readable pictures down to about 6 dB.
This module finds the header (and therefore the mode and the exact start
of the image) on its own, then hands the audio to the decoder with those
already known.

SSTV VIS header timeline, seconds from its start (all confirmed against
`sstv.encode()`'s own output for every mode in MODE_SECONDS below, not
just recalled):

    leader 1900 Hz 0.300 | break 1200 Hz 0.010 | leader 1900 Hz 0.300 |
    start bit 1200 Hz 0.030 | 8 bits x 0.030 (1100 Hz = 1, 1300 Hz = 0:
    7 data bits LSB first + one EVEN parity bit) | stop bit 1200 Hz 0.030
    -> image data begins at 0.910 s

Method: a sliding 30 ms rectangular-window tone-power measurement (its
FFT null spacing is 33.3 Hz, so the 100 Hz-spaced 1100/1200/1300 tones
don't leak into each other) at four tones, each also probed at seven
frequency offsets (+-60 Hz in 20 Hz steps) so an SSB-mistuned signal
still scores. A candidate start time is scored by how strongly the
expected tone dominates the four-tone total at every point of the
timeline above; the bit values are then read directly off the winning
candidate and must pass the parity check AND be a VIS code this module
knows (both are cheap, strong filters against noise coincidences).

Verified before shipping: synthetic Martin M1 / Scottie S1 / Robot 36
mixed into 60 s of REAL recorded rig noise were all found at the correct
start time (to within 5 ms) down to -3 dB full-band SNR, tolerating
+-60 Hz of mistuning. Against a separate real 9.5 minute recording of
14.230 MHz, the detector (min_score 0.62) raised 2 confident false
detections (scores 0.90, 0.90) and 1 marginal one -- one of the two turned
out to be a genuine, too-weak Robot 36 transmission, the other an
apparent noise coincidence. A header alone is therefore NOT sufficient
evidence of a real image; sstv_rx.py always confirms with a decode-time
noise gate before showing anything.
"""
import numpy as np

RATE = 48000
WIN = 1440            # 30 ms rectangular window
HOP = 240             # 5 ms
FREQS = (1100.0, 1200.0, 1300.0, 1900.0)
OFFSETS = (-60, -40, -20, 0, 20, 40, 60)   # tolerated SSB mistuning, Hz
IMAGE_START_S = 0.910                      # header start -> first image sample

# VIS code -> name of the matching `sstv.Mode` attribute. Robot 8 BW and
# Pasokon P7 are deliberately absent: the decoder package has no mode for
# the first and P7's 8-bit code doesn't fit the 7-bit table below.
VIS_MODES = {
    8: "ROBOT_36", 12: "ROBOT_72", 40: "MARTIN_2", 44: "MARTIN_1",
    55: "WRASSE_SC2_180", 56: "SCOTTIE_2", 60: "SCOTTIE_1", 76: "SCOTTIE_DX",
    93: "PD_50", 94: "PD_290", 95: "PD_120", 96: "PD_180", 97: "PD_240",
    98: "PD_160", 99: "PD_90", 113: "PASOKON_P3", 114: "PASOKON_P5",
}

# Seconds of image data after the header, MEASURED by encoding a blank
# frame with `sstv.encode()` for each mode and subtracting the encoder's
# fixed 0.8 s lead-in plus the 0.910 s header -- they match the commonly
# published figures (Martin M1 114.3 s, Scottie S1 109.6 s, Robot 36
# 36.0 s, PD 120 126.1 s), not recalled from memory.
MODE_SECONDS = {
    "ROBOT_36": 36.0, "ROBOT_72": 72.0, "MARTIN_1": 114.3, "MARTIN_2": 58.1,
    "SCOTTIE_1": 109.6, "SCOTTIE_2": 71.1, "SCOTTIE_DX": 268.9,
    "WRASSE_SC2_180": 182.0, "PASOKON_P3": 203.0, "PASOKON_P5": 304.6,
    "PD_50": 49.7, "PD_90": 90.0, "PD_120": 126.1, "PD_160": 160.9,
    "PD_180": 187.1, "PD_240": 248.0, "PD_290": 288.7,
}

MODE_LABELS = {
    "ROBOT_36": "Robot 36", "ROBOT_72": "Robot 72", "MARTIN_1": "Martin 1",
    "MARTIN_2": "Martin 2", "SCOTTIE_1": "Scottie 1", "SCOTTIE_2": "Scottie 2",
    "SCOTTIE_DX": "Scottie DX", "WRASSE_SC2_180": "Wraase SC2-180",
    "PASOKON_P3": "Pasokon P3", "PASOKON_P5": "Pasokon P5",
    "PD_50": "PD 50", "PD_90": "PD 90", "PD_120": "PD 120", "PD_160": "PD 160",
    "PD_180": "PD 180", "PD_240": "PD 240", "PD_290": "PD 290",
}


def _tone_power(x, rate=RATE):
    """(n_offsets, 4, n_frames) tone power per 5 ms hop."""
    n = (len(x) - WIN) // HOP + 1
    t = np.arange(WIN) / rate
    bins = np.array([f + o for o in OFFSETS for f in FREQS])
    basis = np.exp(-2j * np.pi * np.outer(t, bins)).astype(np.complex64)
    out = np.empty((len(bins), n), dtype=np.float32)
    xs = np.asarray(x, dtype=np.float32)
    view = np.lib.stride_tricks.sliding_window_view(xs, WIN)[::HOP]
    for a in range(0, n, 2000):
        seg = view[a:a + 2000]
        out[:, a:a + len(seg)] = (np.abs(seg @ basis) ** 2).T
    return out.reshape(len(OFFSETS), 4, n)


def _seg_mean(cs, lo, hi, n_starts):
    """Mean of frames [u+lo, u+hi) for every start index u, via cumsum."""
    return (cs[:, hi:hi + n_starts] - cs[:, lo:lo + n_starts]) / (hi - lo)


def detect_vis(x, rate=RATE, min_score=0.62, min_gap_s=8.0):
    """Find VIS headers in mono audio `x` (any numeric array, 48 kHz).

    Returns [(start_s, image_start_s, vis_code, mode_name, score,
    offset_hz)], strongest first; times are seconds from the start of `x`.
    A detection is only returned once the whole 0.91 s header fits inside
    `x`. Never raises."""
    try:
        if rate != RATE or len(x) < RATE * 2:
            return []
        P = _tone_power(x, rate)
        tot = P.sum(axis=1, keepdims=True) + 1e-9
        D = P / tot                                    # tone dominance, per offset
        n = D.shape[2]
        span = 178                                     # frames used from a header's start
        ns = n - span
        if ns <= 0:
            return []
        cs = np.concatenate(
            [np.zeros(D.shape[:2] + (1,), np.float32), np.cumsum(D, axis=2, dtype=np.float64)], axis=2)
        best = np.full(ns, -1.0)
        best_off = np.zeros(ns, dtype=int)
        best_bits = np.zeros((ns, 8), dtype=int)
        for oi in range(len(OFFSETS)):
            c = cs[oi]                                 # (4, n+1)
            l1 = _seg_mean(c[3:4], 0, 55, ns)[0]       # leader 1 (0.00-0.30 s)
            l2 = _seg_mean(c[3:4], 62, 117, ns)[0]     # leader 2 (0.31-0.61 s)
            st = _seg_mean(c[1:2], 122, 123, ns)[0]    # start bit
            sp = _seg_mean(c[1:2], 176, 177, ns)[0]    # stop bit
            bit_scores, bit_val = [], []
            for i in range(8):
                u0 = 128 + 6 * i
                d1 = _seg_mean(c[0:1], u0, u0 + 1, ns)[0]
                d0 = _seg_mean(c[2:3], u0, u0 + 1, ns)[0]
                bit_scores.append(d1 + d0)
                bit_val.append((d1 > d0).astype(int))
            bs = np.mean(bit_scores, axis=0)
            score = 0.30 * l1 + 0.30 * l2 + 0.10 * st + 0.10 * sp + 0.20 * bs
            better = score > best
            best = np.where(better, score, best)
            best_off = np.where(better, oi, best_off)
            best_bits = np.where(better[:, None], np.stack(bit_val, axis=1), best_bits)
        found, taken = [], []
        for u in np.argsort(-best)[:2000]:
            if best[u] < min_score:
                break
            t0 = float(u) * HOP / rate
            if any(abs(t0 - s) < min_gap_s for s in taken):
                continue
            bits = best_bits[u]
            parity_ok = (int(bits[:7].sum()) + int(bits[7])) % 2 == 0
            code = int(sum(int(b) << i for i, b in enumerate(bits[:7])))
            if not parity_ok or code not in VIS_MODES:
                continue
            taken.append(t0)
            found.append((t0, t0 + IMAGE_START_S, code, VIS_MODES[code],
                          float(best[u]), OFFSETS[int(best_off[u])]))
        return found
    except Exception:
        return []
