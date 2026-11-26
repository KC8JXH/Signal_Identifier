"""SSTV (slow-scan television) and weather-fax detection.

An SSTV picture is sent as one FM tone that sweeps between "black" (1500 Hz)
and "white" (2300 Hz), with a short 1200 Hz sync pulse at the start of every
scan line. Each mode sends its lines at a fixed rate (Martin M1: one every
446.4 ms), so the tone's frequency repeats almost exactly once per line.

We follow the tone millisecond by millisecond and look for that repetition.
It works even when the radio is mistuned, because the line rate doesn't
depend on where the tones land in the audio; the sync pulse then tells us
how far off the tuning is.
"""

from __future__ import annotations

import numpy as np

# Scan-line periods in ms (PD modes send two lines per period), and how long
# each mode's 1200 Hz sync pulse lasts.
LINE_PERIODS = [
    (446.446, "Martin M1"), (226.798, "Martin M2"),
    (428.220, "Scottie S1"), (277.692, "Scottie S2"), (1050.3, "Scottie DX"),
    (150.0, "Robot 36"), (300.0, "Robot 72"),
    (388.16, "PD50"), (703.04, "PD90"), (508.48, "PD120"), (804.416, "PD160"),
    (754.24, "PD180"), (711.0437, "Wraase SC2-180"),
    (500.0, "WEFAX 120 lpm"), (1000.0, "PD240 or WEFAX 60 lpm"),
]
SYNC_MS = {"Martin": 4.862, "Scottie": 9.0, "Robot": 9.0, "PD": 20.0, "Wraase": 5.5}
SYNC_HZ = 1200.0          # SSTV sync tone
BLACK_HZ, WHITE_HZ = 1500.0, 2300.0
BLOCK_S = 3.0             # judge the line rate in 3-second blocks
MIN_SCORE = 0.45          # line-to-line similarity needed (0..1)


def tone_track(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    """Frequency of the strongest tone between lo and hi Hz, once per millisecond."""
    n = len(x)
    Z = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1 / fs)
    keep = (f >= lo) & (f <= hi)
    Z = np.where(keep, 2 * Z, 0)                    # analytic signal, band-limited
    z = np.fft.ifft(Z)
    d = z[1:] * np.conj(z[:-1])                     # phase step per sample
    step = max(1, int(round(fs / 1000)))
    m = len(d) // step
    dm = d[:m * step].reshape(m, step).sum(axis=1)  # amplitude-weighted, robust in noise
    dm = np.convolve(dm, np.ones(3) / 3, mode="same")
    return np.angle(dm) * fs / (2 * np.pi)


def _autocorr(y: np.ndarray) -> np.ndarray:
    y = y - y.mean()
    n = len(y)
    Y = np.fft.rfft(y, 2 * n)
    ac = np.fft.irfft(Y * np.conj(Y))[:n]
    return ac / ac[0] if ac[0] > 0 else np.zeros(n)


def _peak_near(ac: np.ndarray, lag_ms: float) -> tuple[float, float]:
    """Strongest repetition within +/-1.5 % of lag_ms: (similarity, exact lag)."""
    lo, hi = int(lag_ms * 0.985), int(lag_ms * 1.015) + 2
    if hi + 1 >= len(ac):
        return 0.0, 0.0
    i = lo + int(np.argmax(ac[lo:hi]))
    if i in (lo, hi - 1):
        return 0.0, 0.0                             # still rising: the real peak is elsewhere
    d = ac[i - 1] - 2 * ac[i] + ac[i + 1]
    lag = i + (0.5 * (ac[i - 1] - ac[i + 1]) / d if d < 0 else 0.0)
    return float(ac[i]), float(lag)


def _best_window(spec_lin: np.ndarray, freqs: np.ndarray, width: float = 1300.0) -> tuple[float, float]:
    """The `width`-Hz slice of the passband with the most energy."""
    df = freqs[1] - freqs[0]
    k = max(1, int(width / df))
    sel = (freqs >= 100) & (freqs <= 3500)
    p = np.where(sel, spec_lin, 0.0)
    c = np.convolve(p, np.ones(k), mode="valid")
    i = int(np.argmax(c))
    return float(freqs[i]), float(freqs[min(len(freqs) - 1, i + k)])


def _tuning_offset(track: np.ndarray, a: int, b: int, nominal_ms: float, sync_ms: float = 9.0) -> float:
    """How far the picture's tones are from where they belong (Hz; negative =
    heard lower than normal, i.e. the radio is tuned too high in USB).

    Stack the scan lines on top of each other. If a short dip shows up well
    below the rest, it's the 1200 Hz sync pulse. If not (the radio's filter
    often cuts the sync off when the signal is tuned low), use the picture's
    black level, which belongs at 1500 Hz."""
    y = track[a:b]
    lo_v, hi_v = np.percentile(y, [1, 98])
    ac = _autocorr(np.clip(y, lo_v, hi_v))
    # The sound card's clock is never exact: measure the real line period.
    lo_i, hi_i = int(nominal_ms * 0.99), int(nominal_ms * 1.01) + 2
    if hi_i + 1 < len(ac):
        i = lo_i + int(np.argmax(ac[lo_i:hi_i]))
        d = ac[i - 1] - 2 * ac[i] + ac[i + 1]
        per = i + (0.5 * (ac[i - 1] - ac[i + 1]) / d if d < 0 else 0.0)
        n = int(len(y) // per) - 1
        if n >= 3:
            p = int(per)
            starts = (np.arange(n) * per).astype(int)
            stacked = np.mean([y[s:s + p] for s in starts], axis=0)
            m = float(stacked.min())
            dip_ms = int((stacked < m + 100).sum())
            if dip_ms <= sync_ms + 10 and np.percentile(stacked, 10) - m >= 200:
                # the middle of the dip, clear of the edges blurred by filtering
                return float(np.median(np.sort(stacked)[:max(1, int(sync_ms * 0.6))])) - SYNC_HZ
    return float(lo_v) - BLACK_HZ


def detect(x: np.ndarray, fs: float, spec_lin: np.ndarray, freqs: np.ndarray):
    """Look for an SSTV/WEFAX picture. Returns None, or a dict with
    mode, sync_hz (where the sync tone is heard), offset_hz (how far the
    tones are from where they should be), score and the band it occupies."""
    if len(x) < fs * 2 * BLOCK_S:
        return None
    lo, hi = _best_window(spec_lin, freqs)
    # reach well below the picture band: the sync pulse sits 300 Hz under "black"
    track = tone_track(x, fs, max(60.0, lo - 450), min(fs / 2 - 50, hi + 100))
    # A picture sweeps across hundreds of Hz. Narrow signals with a steady
    # rhythm (PSK31 idling repeats every 32 ms, and 14 x 32 = 448 ms) don't.
    band = (freqs >= lo) & (freqs <= hi)
    c = np.cumsum(spec_lin[band])
    if c[-1] <= 0:
        return None
    f_band = freqs[band]
    if f_band[np.searchsorted(c, 0.95 * c[-1])] - f_band[np.searchsorted(c, 0.05 * c[-1])] < 250:
        return None
    blk = int(BLOCK_S * 1000)
    scores = []                                     # per block: {mode: similarity}
    for s0 in range(0, len(track) - blk + 1, blk):
        y = track[s0:s0 + blk]
        p2, p98 = np.percentile(y, [2, 98])
        if p98 - p2 < 250:                          # a steady tone, not a picture
            scores.append({})
            continue
        ac = _autocorr(np.clip(y, p2 - 50, p98 + 50))
        sc = {}
        for per, name in LINE_PERIODS:
            v, lag = _peak_near(ac, per)
            # The repeat must land on this mode's line period (sound-card clocks
            # are good to a few tenths of a percent), not merely near it.
            if v >= MIN_SCORE and abs(lag - per) <= per * 0.006:
                sc[name] = v
        scores.append(sc)
    found = {}
    for per, name in LINE_PERIODS:
        good = [i for i, sc in enumerate(scores) if name in sc]
        if len(good) >= 2:                          # at least 6 s of picture
            found[name] = (float(np.mean([scores[i][name] for i in good])), per, name, good)
    if not found:
        return None
    best = max(found.values())
    # Fast modes also repeat at whole multiples of their line period (Robot 36
    # at 150, 300, 450 ms...): if a shorter period divides the winner and
    # scores nearly as well, that's the real mode.
    for cand in sorted(found.values(), key=lambda c: c[1]):
        k = best[1] / cand[1]
        if cand[1] < best[1] and round(k) >= 2 and abs(k - round(k)) < 0.015 * k and cand[0] >= 0.8 * best[0]:
            best = cand
            break
    score, per, name, good = best
    # Stack the lines of the best stretch: the lowest point is the sync pulse.
    i0 = max(good, key=lambda i: scores[i][name])
    sync_ms = next((v for k, v in SYNC_MS.items() if name.startswith(k)), 9.0)
    offset = _tuning_offset(track, i0 * blk, min(len(track), i0 * blk + 2 * blk), per, sync_ms)
    offset = 0.0 if abs(offset) < 60 else round(offset / 10) * 10
    sync = SYNC_HZ + offset
    if name.startswith("WEFAX"):
        offset = 0.0                                # fax has no 1200 Hz sync to measure from
    return {"mode": name, "sync_hz": sync, "offset_hz": offset, "score": score,
            "lo_hz": SYNC_HZ + offset - 50, "hi_hz": WHITE_HZ + offset + 50,
            "seconds": len(good) * BLOCK_S}
