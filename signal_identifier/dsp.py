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


def noise_floor_db(spec_db: np.ndarray, smooth_bins: int) -> np.ndarray:
    """Estimate the mean noise power (dB) in each frequency bin.

    Takes a low percentile over time (finds the quiet moments), converts it to
    an estimate of the mean noise level, then smooths across frequency so a
    signal that never stops (a carrier, RTTY) doesn't hide its own noise floor.
    """
    p5 = np.percentile(spec_db, 5, axis=0)
    # For pure noise the 5th percentile sits ~12.9 dB below the mean, so this
    # still works when a signal is present 90% of the time.
    mean_est = p5 + 12.9
    return running_median(mean_est, smooth_bins)


def undo_agc(spec_db: np.ndarray, freqs: np.ndarray, f_lo: float = 150, f_hi: float = 3500) -> np.ndarray:
    """Cancel the receiver's AGC "pumping".

    When a strong signal keys down, the radio's AGC turns the whole audio band
    down, and the background noise comes back up between dots and dashes. That
    makes everything flicker in step with the strong signal, which can look
    like speech. Here we track the background level in each moment (a low
    percentile across the band, so signals barely affect it) and remove its
    ups and downs.
    """
    band = (freqs >= f_lo) & (freqs <= f_hi)
    if band.sum() < 8 or len(spec_db) < 3:
        return spec_db
    bg = np.percentile(spec_db[:, band], 25, axis=1)
    k = 3
    bg = np.convolve(np.pad(bg, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    return spec_db - (bg - np.median(bg))[:, None]
