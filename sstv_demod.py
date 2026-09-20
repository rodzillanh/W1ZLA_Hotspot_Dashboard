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


def _scan_starts(spec):
    """Scan start times relative to the sync pulse CENTRE, per channel."""
    scan, gap, sync = spec["scan_ms"] / 1000.0, spec["gap_ms"] / 1000.0, spec["sync_ms"] / 1000.0
    if spec["sync_first"]:
        base = sync / 2 + gap                # first scan begins after the sync pulse + its porch
        return {c: base + i * (scan + gap) for i, c in enumerate(spec["order"])}
    # Scottie: [gap][G][gap][B][sync][gap][R] -- no gap between B and the sync pulse
    b_start = -sync / 2 - scan
    return {"B": b_start, "G": b_start - gap - scan, "R": sync / 2 + gap}


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
        starts = _scan_starts(spec)
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


# --------------------------------------------------------------------------
# Joining a picture that is already in progress
#
# A VIS header is only sent once, at the start. If the receiver starts
# listening (or the rig is tuned onto a station) partway through a picture,
# the header was never heard -- and worse, loud picture content can imitate a
# header (real case, 2026-09: a Scottie 2 picture in progress read as a
# "PD 120" header at 0.63, decoded to static and was dropped, although the
# audio was perfectly clear). But every line of every mode carries a 1200 Hz
# sync pulse at that mode's own line period, so the sync train alone says
# which mode is on the air and where every line starts.

# Sync-pulse spacing per mode in milliseconds, MEASURED by encoding a random
# picture in each mode with `sstv.encode()` and timing the pulses (1 ms
# resolution), not recalled -- they agree with the published line times.
LINE_PERIODS_MS = {
    "ROBOT_36": 150.0, "ROBOT_72": 300.0, "MARTIN_2": 227.0, "MARTIN_1": 446.5,
    "WRASSE_SC2_180": 711.0, "SCOTTIE_2": 278.0, "SCOTTIE_1": 428.0, "SCOTTIE_DX": 1050.0,
    "PD_50": 388.0, "PD_290": 937.0, "PD_120": 508.5, "PD_180": 754.0, "PD_240": 1000.0,
    "PD_160": 804.5, "PD_90": 703.0, "PASOKON_P3": 409.5, "PASOKON_P5": 614.0,
}


def sync_pulses(x, offset_hz=0.0):
    """Centre times (s) of the 1200 Hz sync pulses in `x`, at most one per 30 ms."""
    x = np.asarray(x, dtype=np.float32)
    if abs(offset_hz) >= 10 and len(x) >= 2:
        z = _analytic(x) * np.exp(-2j * np.pi * offset_hz * np.arange(len(x), dtype=np.float32) / RATE)
        x = z.real.astype(np.float32)
    dom, times = _sync_dominance(x)
    if dom is None:
        return []
    out = []
    i, n = 0, len(dom)
    while i < n:
        if dom[i] > 0.5:
            j = i
            while j < n and dom[j] > 0.5:
                j += 1
            out.append(float(times[i:j].mean()))
            i = j + 30
        else:
            i += 1
    return out


def identify_by_sync(samples, offset_hz=0.0, min_pulses=10):
    """Which SSTV mode is on the air, judged only by the spacing of its sync
    pulses. Returns {"mode", "first_s", "n", "score"} or None. A mode is
    reported only when at least `min_pulses` pulses sit one line period apart
    (allowing a missed pulse), so noise and picture content don't match."""
    try:
        pulses = sync_pulses(samples, offset_hz)
        if len(pulses) < min_pulses:
            return None
        d = np.diff(pulses)
        best = None
        for name, ms in LINE_PERIODS_MS.items():
            P = ms / 1000.0
            one = np.abs(d / P - 1.0) < 0.012
            two = np.abs(d / (2 * P) - 1.0) < 0.012          # one pulse missed
            n_ok = int(one.sum() + two.sum())
            score = n_ok / len(d)
            if best is None or score > best[0]:
                best = (score, name, int(one.sum()), P)
        score, name, n1, P = best
        if score < 0.6 or n1 < min_pulses - 2:
            return None
        # first pulse of the first regular chain
        idx = next((k for k in range(len(d)) if abs(d[k] / P - 1.0) < 0.012), 0)
        return {"mode": name, "first_s": pulses[idx], "n": n1 + 1, "score": round(score, 2), "period_s": P}
    except Exception as e:
        print(f"[sstv] sync identification failed: {type(e).__name__}: {e}")
        return None


def decode_joined(samples, mode_name, offset_hz=0.0, first_pulse_s=0.0):
    """Decode the part of a picture that was heard, for audio that starts
    MID-picture: line timing comes from the sync pulses alone (the first
    pulse at/after `first_pulse_s` is taken as line 0). Returns an RGB image
    with only the lines actually heard (never padded with black) or None."""
    try:
        spec = MODES.get(mode_name)
        if spec is None:
            return None
        prep = prepare(samples, offset_hz)
        if prep is None:
            return None
        H = spec["height"]
        line = spec["line_ms"] / 1000.0
        audio_s = prep["n"] / RATE
        n_avail = int((audio_s - first_pulse_s) / line) + 2
        fit = _fit_timing(prep["dom"], prep["times"], first_pulse_s, line, min(n_avail, H))
        t0, T = (first_pulse_s, line) if fit is None else (fit[0], fit[1])
        starts = _scan_starts(spec)
        scan = spec["scan_ms"] / 1000.0
        # lines whose every scan lies inside the audio we actually have
        first = 0
        while t0 + first * T + min(starts.values()) < 0.05:
            first += 1
        last = first
        while last < H and t0 + last * T + max(starts.values()) + scan <= audio_s - 0.02:
            last += 1
        # ...and only up to the last line that really had a sync pulse: after the
        # transmission ends the audio goes on (noise), and reading it would append
        # black rows to the picture
        dom, times = prep["dom"], prep["times"]
        seen, misses = first, 0
        for k in range(first, last):
            c = t0 + k * T
            lo, hi = np.searchsorted(times, c - 0.03), np.searchsorted(times, c + 0.03)
            if hi > lo and dom[lo:hi].max() > 0.5:
                seen, misses = k + 1, 0
            else:
                misses += 1
                if misses >= 3:                 # the train has ended; a stray noise peak later doesn't extend it
                    break
        last = min(last, seen)
        if last - first < 24:                  # too few lines to be worth showing
            return None
        im = render(prep, mode_name, timing=(t0, T))
        if im is None:
            return None
        return im.crop((0, first, im.size[0], last))
    except Exception as e:
        print(f"[sstv] joined decode failed: {type(e).__name__}: {e}")
        return None
