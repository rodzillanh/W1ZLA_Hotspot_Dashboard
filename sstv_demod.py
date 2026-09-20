"""Noise-robust SSTV picture demodulator (numpy only) for the SSTV card.

Why this exists next to the `sstv` decoder package: on a REAL, clearly
present Martin 1 transmission recorded from the user's rig (2026-09-19,
~+5 dB signal-to-noise, sync pulses measured strong on all 250 lines, line
clock within 12 ppm of nominal) that package produced a streaky, barely
recognisable picture -- it loses line synchronisation in noise -- while the
straightforward method below recovered a clear face and readable
"CQ SSTV" text from the SAME audio. The difference is that this module
recovers the line timing GLOBALLY: it locates the strong 1200 Hz sync
pulses, robustly fits one line period + start time across the whole
picture (SSTV line clocks are extremely stable), and then reads every pixel
at its computed time, instead of re-synchronising line by line where noise
can knock a single line out of place.

Method:
  1. Band-limit to the SSTV tone range and take the analytic signal; the
     instantaneous frequency (Hz) and its envelope are the raw demodulation.
  2. Find sync pulses as moments where 1200 Hz dominates the four SSTV tones,
     fit (first pulse, line period) with an outlier-rejecting least squares.
  3. For every scan of every line, average the instantaneous frequency over
     that pixel's time window (weighted by envelope^2 so faded/noisy samples
     count for little, and clipped to the valid tone range so phase-slip
     spikes can't dominate), then map 1500..2300 Hz onto 0..255.

Modes supported here are the sequential-RGB ones whose line layout is
verified against `sstv.encode()` (see MODES): Martin 1/2 and Scottie 1/2.
Anything else returns None and the caller falls back to the `sstv` package.
Timing tables were validated by round-tripping encoder output, not just
recalled -- see the round-trip check in the SSTV notes in CLAUDE.md.
"""
import numpy as np
from PIL import Image

RATE = 48000
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0

# name -> layout. Times in milliseconds. `scans` lists (offset_of_scan_start_from_sync_START, channel);
# each scan is `scan_ms` long and holds `width` pixels. `sync_first` says whether the sync pulse begins
# the line (Martin) or sits between the 2nd and 3rd scan (Scottie).
MODES = {
    "MARTIN_1": dict(width=320, height=256, scan_ms=146.432, sync_ms=4.862, line_ms=446.446, sync_first=True,
                     order=("G", "B", "R"), gap_ms=0.572, pre_ms=4.862 + 0.572),
    "MARTIN_2": dict(width=320, height=256, scan_ms=73.216, sync_ms=4.862, line_ms=226.798, sync_first=True,
                     order=("G", "B", "R"), gap_ms=0.572, pre_ms=4.862 + 0.572),
    "SCOTTIE_1": dict(width=320, height=256, scan_ms=138.24, sync_ms=9.0, line_ms=428.22, sync_first=False,
                      order=("G", "B", "R"), gap_ms=1.5, pre_ms=1.5),
    "SCOTTIE_2": dict(width=320, height=256, scan_ms=88.064, sync_ms=9.0, line_ms=277.692, sync_first=False,
                      order=("G", "B", "R"), gap_ms=1.5, pre_ms=1.5),
    # Scottie DX is deliberately NOT here: its layout (345.6 ms scans, 1050.3 ms lines as recalled) did not
    # round-trip against `sstv.encode()` (mean error 30 vs the library's 1.0), so it is left to the package
    # rather than shipping a timing table nobody has verified.
}


def supported(mode_name):
    return mode_name in MODES


def _analytic(v):
    n = len(v)
    X = np.fft.fft(v)
    h = np.zeros(n, dtype=np.float32)
    h[0] = 1
    h[1:(n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    return np.fft.ifft(X * h)


def _bandpass(v, lo=1000.0, hi=2600.0):
    n = len(v)
    X = np.fft.rfft(v)
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    m = (np.clip((f - lo) / 60.0 + 0.5, 0, 1) * np.clip((hi - f) / 60.0 + 0.5, 0, 1)).astype(np.float32)
    return np.fft.irfft(X * m, n)


def _fm_track(x):
    """(instantaneous frequency in Hz, envelope) per sample."""
    z = _analytic(_bandpass(np.asarray(x, dtype=np.float32)))
    ph = np.unwrap(np.angle(z))
    f = np.diff(ph) * (RATE / (2 * np.pi))
    f = np.concatenate([f, f[-1:]])
    return f.astype(np.float32), np.abs(z).astype(np.float32)


def _sync_dominance(x, hop_ms=1.0, win_ms=5.0):
    """Per-hop measure of how much of the window's TOTAL energy is a 1200 Hz tone (the sync tone): ~1.0
    for a real sync pulse, a few thousandths for a picture tone (1500-2300 Hz) leaking through the
    window's sidelobes. Measured against the window's whole energy, NOT against a few fixed comparison
    tones -- picture tones sweep continuously, and one that lands on the nulls of every comparison tone
    (e.g. 2100 Hz against 1500/1900/2300 with a 5 ms window) once made leakage look like a perfect pulse."""
    win = int(win_ms / 1000 * RATE)
    hop = int(hop_ms / 1000 * RATE)
    t = np.arange(win) / RATE
    tone = np.exp(-2j * np.pi * 1200.0 * t).astype(np.complex64)
    n = (len(x) - win) // hop
    if n <= 0:
        return None, None
    view = np.lib.stride_tricks.sliding_window_view(np.asarray(x, dtype=np.float32), win)[::hop][:n]
    out = np.empty(n, dtype=np.float32)
    for a in range(0, n, 4000):
        blk = view[a:a + 4000]
        num = np.abs(blk @ tone) ** 2
        energy = (blk.astype(np.float32) ** 2).sum(axis=1)
        out[a:a + 4000] = num / (0.5 * win * energy + 1e-9)
    times = (np.arange(n) * hop + win / 2) / RATE
    return out, times


def _fit_timing(dom, times, first_guess_s, period_s, n_lines):
    """Robust fit of sync-pulse centre times: t_k = t0 + k*T. Returns (t0, T, n_used) or None."""
    ks, ts = [], []
    for k in range(n_lines):
        c = first_guess_s + k * period_s
        lo, hi = np.searchsorted(times, c - 0.04), np.searchsorted(times, c + 0.04)
        if hi >= len(times) or hi - lo < 8:
            break
        seg = dom[lo:hi]
        if seg.max() > 0.50:
            ks.append(k)
            ts.append(times[lo + int(np.argmax(seg))])
    if len(ks) < 0.4 * n_lines:
        return None
    ks = np.array(ks, dtype=np.float64)
    ts = np.array(ts, dtype=np.float64)
    # Robust start: the MEDIAN offset from a nominal clock ignores the wrong picks a plain least
    # squares would be dragged by, then progressively tighter inlier windows refine both T and t0.
    T = period_s
    t0 = float(np.median(ts - ks * T))
    keep = np.ones(len(ks), dtype=bool)
    for tol in (0.012, 0.006, 0.003, 0.003):
        keep = np.abs(ts - (t0 + T * ks)) < tol
        if keep.sum() < max(20, 0.3 * len(ks)):
            return None
        A = np.vstack([ks[keep], np.ones(keep.sum())]).T
        T, t0 = np.linalg.lstsq(A, ts[keep], rcond=None)[0]
    if abs(T / period_s - 1) > 0.01:         # a >1% clock error isn't a real SSTV line clock: distrust the fit
        return None
    return float(t0), float(T), int(keep.sum())


def prepare(samples, offset_hz=0.0):
    """The expensive, mode-independent part: FM track + envelope + sync-dominance curve for audio that
    STARTS at the first image sample. Returns a dict or None. Reused across renders/timing trials."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if len(x) < RATE * 5:
            return None
        if abs(offset_hz) >= 10:             # undo mistuning: shift the whole audio
            z = _analytic(x) * np.exp(-2j * np.pi * offset_hz * np.arange(len(x), dtype=np.float32) / RATE)
            x = z.real.astype(np.float32)
        f, env = _fm_track(x)
        dom, times = _sync_dominance(x)
        if dom is None:
            return None
        fc = np.clip(f, 1300.0, 2500.0).astype(np.float64)
        w = env.astype(np.float64) ** 2
        return {"n": len(fc), "dom": dom, "times": times,
                "Cw": np.concatenate([[0.0], np.cumsum(w)]), "Cf": np.concatenate([[0.0], np.cumsum(fc * w)])}
    except Exception as e:
        print(f"[sstv] own demodulator failed: {type(e).__name__}: {e}")
        return None


def render(prep, mode_name, timing=None, core=(0.0, 1.0)):
    """Cheap part: fit the line timing (unless `timing` = (t0, T) is given) and read every pixel."""
    try:
        spec = MODES.get(mode_name)
        if spec is None or prep is None:
            return None
        H, W = spec["height"], spec["width"]
        scan, gap = spec["scan_ms"] / 1000.0, spec["gap_ms"] / 1000.0
        line = spec["line_ms"] / 1000.0
        sync = spec["sync_ms"] / 1000.0
        # Predicted centre of the first sync pulse.
        if spec["sync_first"]:
            guess = sync / 2
        else:                                # Scottie: [gap] G scan [gap] B scan, THEN the sync pulse
            guess = spec["pre_ms"] / 1000.0 + scan + gap + scan + sync / 2
        if timing is not None:
            t0, T = timing
        else:
            fit = _fit_timing(prep["dom"], prep["times"], guess, line, H)
            t0, T = (guess, line) if fit is None else (fit[0], fit[1])     # no sync evidence: nominal timing
        # scan start times relative to the sync pulse CENTRE, per channel
        if spec["sync_first"]:
            base = sync / 2 + gap            # first scan begins after the sync pulse + its porch
            starts = {c: base + i * (scan + gap) for i, c in enumerate(spec["order"])}
        else:
            # Scottie: [gap][G][gap][B][sync][gap][R] -- no gap between B and the sync pulse
            b_start = -sync / 2 - scan
            starts = {"B": b_start, "G": b_start - gap - scan, "R": sync / 2 + gap}
        Cf, Cw, n = prep["Cf"], prep["Cw"], prep["n"]
        img = np.zeros((H, W, 3), dtype=np.float64)
        ch_index = {"R": 0, "G": 1, "B": 2}
        px = np.arange(W)
        lo, hi = core
        for c, s0 in starts.items():
            tline = t0 + np.arange(H) * T + s0                          # (H,)
            # read only the CORE of each pixel's window: its edges are where the tone is still moving
            # from the previous pixel's value (and where filter ringing sits)
            ea = tline[:, None] + (px[None, :] + lo) * (scan / W)
            eb = tline[:, None] + (px[None, :] + hi) * (scan / W)
            a = np.clip((ea * RATE).astype(np.int64), 0, n)
            b = np.minimum(np.maximum(np.clip((eb * RATE).astype(np.int64), 0, n), a + 1), n)
            num = Cf[b] - Cf[a]
            den = Cw[b] - Cw[a]
            val = np.where(den > 1e-9, num / np.maximum(den, 1e-9), FREQ_BLACK)
            img[:, :, ch_index[c]] = np.clip((val - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255.0, 0, 255)
        return Image.fromarray(img.astype(np.uint8), "RGB")
    except Exception as e:
        print(f"[sstv] own demodulator failed: {type(e).__name__}: {e}")
        return None


def decode(samples, mode_name, offset_hz=0.0):
    """Decode audio that STARTS at the first image sample. Returns an RGB PIL image or None.
    Never raises. `offset_hz` is the SSB mistuning measured from the header."""
    if mode_name not in MODES:
        return None
    spec = MODES[mode_name]
    if len(samples) < RATE * min(spec["line_ms"] / 1000.0 * spec["height"], 20):
        return None
    return render(prepare(samples, offset_hz), mode_name)
