"""Basic signal processing helpers (numpy only, no scipy needed)."""

from __future__ import annotations

import numpy as np

TARGET_RATE = 12000  # we analyse audio at roughly 12 kHz; plenty for a 3 kHz SSB passband


def lowpass_fir(cutoff: float, fs: float, taps: int = 129) -> np.ndarray:
    """Windowed-sinc low-pass filter."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(taps)
    return h / h.sum()


def decimate(x: np.ndarray, fs: float) -> tuple[np.ndarray, float]:
    """Bring audio down to about TARGET_RATE. Returns (samples, new_rate)."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    factor = max(1, int(round(fs / TARGET_RATE)))
    if factor == 1:
        return x, float(fs)
    h = lowpass_fir(0.45 * fs / factor, fs)
    y = np.convolve(x, h, mode="same")[::factor]
    return y, fs / factor


def stft_db(x: np.ndarray, nfft: int, hop: int) -> np.ndarray:
    """Power spectrogram in dB, shape (frames, nfft//2+1)."""
    if len(x) < nfft:
        x = np.pad(x, (0, nfft - len(x)))
    nframes = 1 + (len(x) - nfft) // hop
    idx = np.arange(nfft)[None, :] + hop * np.arange(nframes)[:, None]
    frames = x[idx] * np.hanning(nfft)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    return 10 * np.log10(spec + 1e-20)


def running_median(v: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width) | 1)
    pad = width // 2
    vp = np.pad(v, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(vp, width)
    return np.median(windows, axis=1)


def passband_mask(spec_db: np.ndarray, freqs: np.ndarray, f_lo: float = 100, f_hi: float = 3800) -> np.ndarray:
    """Which frequencies the radio's filter actually lets through.

    Outside the filter the audio is nearly silent (only the sound chip's own
    hiss). We find the level of the band noise inside the filter and keep
    every frequency within 15 dB of it.
    """
    df = freqs[1] - freqs[0]
    w = max(3, int(300 / df))   # smooth over 300 Hz so single signals don't count
    sel = (freqs >= f_lo) & (freqs <= f_hi)
    if not sel.any():
        return sel
    # Two views, each fooled by something different:
    #  * the typical level (median) - fooled by a data signal that's on most of the time
    #  * the quiet moments (10th percentile) - fooled by heavy AGC compression
    # Inside the filter if either says so, but never down at the near-silent
    # level outside the filter.
    med = running_median(np.median(spec_db, axis=0), w)
    quiet = running_median(np.percentile(spec_db, 10, axis=0), w)
    top_med, top_q = np.max(med[sel]), np.max(quiet[sel])
    by_med = med >= top_med - 15
    by_quiet = (quiet >= top_q - 15) & (med >= top_med - 30)
    return sel & (by_med | by_quiet)


def _running_nanmedian(v: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width) | 1)
    pad = width // 2
    vp = np.pad(v, pad, mode="constant", constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(vp, width)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            out = np.nanmedian(windows, axis=1)
    return np.where(np.isnan(out), v, out)


def noise_floor_db(spec_db: np.ndarray, smooth_bins: int, mask: np.ndarray | None = None) -> np.ndarray:
    """Estimate the mean noise power (dB) in each frequency bin.

    Takes a low percentile over time (finds the quiet moments), converts it to
    an estimate of the mean noise level, then smooths across frequency (only
    within the radio's passband) so a signal that never stops (a carrier,
    RTTY) doesn't hide its own noise floor.
    """
    p5 = np.percentile(spec_db, 5, axis=0)
    # For pure noise the 5th percentile sits ~12.9 dB below the mean, so this
    # still works when a signal is present 90% of the time.
    mean_est = p5 + 12.9
    if mask is None:
        return running_median(mean_est, smooth_bins)
    v = np.where(mask, mean_est, np.nan)
    sm = _running_nanmedian(v, smooth_bins)
    return np.where(mask, sm, mean_est)


def undo_agc(spec_db: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Cancel the receiver's AGC "pumping".

    When a strong signal keys down, the radio's AGC turns the whole passband
    down, and the background noise comes back up between dots and dashes.
    That makes everything flicker in step with the strong signal, which can
    look like speech. We track the background level of each moment inside the
    filter passband (a low percentile, so signals barely affect it) and remove
    its ups and downs there. Outside the filter nothing is touched.
    """
    if mask.sum() < 8 or len(spec_db) < 3:
        return spec_db
    # The quietest fifth of the passband in each moment = the background.
    bg = np.percentile(spec_db[:, mask], 20, axis=1)
    k = 3
    bg = np.convolve(np.pad(bg, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    out = spec_db.copy()
    out[:, mask] -= (bg - np.median(bg))[:, None]
    return out
