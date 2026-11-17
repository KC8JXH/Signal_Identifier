"""Make fake receiver audio for each mode (used by the Demo button and the tests)."""

from __future__ import annotations

import numpy as np

FS = 12000
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
    "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", "/": "-..-.",
}


def noise(seconds: float, level: float = 0.05, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # band-limited-ish noise like a receiver
    n = rng.normal(0, level, int(seconds * FS))
    return n


def _smooth_gate(gate: np.ndarray, ms: float = 5) -> np.ndarray:
    k = max(1, int(ms / 1000 * FS))
    return np.convolve(gate, np.ones(k) / k, mode="same")


def cw(seconds=15, freq=700, wpm=20, amp=0.3, text="CQ CQ DE KC8JXH KC8JXH K   "):
    dit = int(1.2 / wpm * FS)
    gate = []
    for ch in text * 10:
        if ch == " ":
            gate += [0] * (dit * 4)
            continue
        for sym in MORSE[ch]:
            gate += [1] * (dit if sym == "." else 3 * dit) + [0] * dit
        gate += [0] * (dit * 2)
    n = int(seconds * FS)
    gate = np.array(gate[:n] + [0] * max(0, n - len(gate)), float)
    t = np.arange(n) / FS
    return amp * _smooth_gate(gate) * np.sin(2 * np.pi * freq * t)


def fsk(symbols, baud, freqs, amp=0.3, smooth=False, start=0.0, seconds=None):
    """Continuous-phase FSK. freqs[symbol] is the tone for each symbol."""
    sps = FS / baud
    n_sym = len(symbols)
    n = int(n_sym * sps)
    idx = np.minimum((np.arange(n) / sps).astype(int), n_sym - 1)
    inst = np.asarray(freqs)[np.asarray(symbols)[idx]].astype(float)
    if smooth:  # Gaussian-ish smoothing of the frequency (GFSK like FT8)
        k = int(sps / 2)
        inst = np.convolve(inst, np.hanning(k) / np.hanning(k).sum(), mode="same")
    phase = 2 * np.pi * np.cumsum(inst) / FS
    sig = amp * np.sin(phase)
    ramp = int(0.01 * FS)
    sig[:ramp] *= np.linspace(0, 1, ramp)
    sig[-ramp:] *= np.linspace(1, 0, ramp)
    if seconds is not None:
        out = np.zeros(int(seconds * FS))
        s = int(start * FS)
        out[s:s + len(sig)] = sig[: max(0, len(out) - s)]
        return out
    return sig


def rtty(seconds=15, mark=2125, shift=170, amp=0.3, seed=1):
    rng = np.random.default_rng(seed)
    baud = 45.45
    syms = rng.integers(0, 2, int(seconds * baud) + 1)
    return fsk(syms, baud, [mark, mark + shift], amp)[: int(seconds * FS)]


def ft8(seconds=15, base=1200, amp=0.2, seed=2, start=0.5):
    rng = np.random.default_rng(seed)
    syms = rng.integers(0, 8, 79)
    tones = base + 6.25 * np.arange(8)
    return fsk(syms, 6.25, tones, amp, smooth=True, start=start, seconds=seconds)


def ft4(seconds=7.5, base=1500, amp=0.2, seed=3, start=0.5):
    rng = np.random.default_rng(seed)
    syms = rng.integers(0, 4, 105)
    tones = base + 20.833 * np.arange(4)
    return fsk(syms, 20.833, tones, amp, smooth=True, start=start, seconds=seconds)


def psk31(seconds=15, freq=1000, amp=0.3, seed=4):
    rng = np.random.default_rng(seed)
    baud = 31.25
    nsym = int(seconds * baud) + 1
    bits = rng.integers(0, 2, nsym)
    # Varicode-ish: a 0 bit means "reverse phase". Ensure plenty of reversals.
    bits[::2] = 0
    phase = np.cumsum(bits == 0) % 2  # 0 or 1
    sps = int(FS / baud)
    n = nsym * sps
    t = np.arange(n) / FS
    amp_env = np.ones(n)
    sign = np.where(phase == 0, 1.0, -1.0)
    bb = np.repeat(sign, sps)
    # raised-cosine transition at each reversal: amplitude goes through zero
    for k in range(1, nsym):
        if sign[k] != sign[k - 1]:
            s = k * sps
            seg = np.cos(np.linspace(0, np.pi, sps))  # +1 -> -1 smoothly
            bb[s - sps // 2: s + sps - sps // 2] = sign[k - 1] * seg
    sig = amp * bb * np.sin(2 * np.pi * freq * t)
    return sig[: int(seconds * FS)]


def carrier(seconds=15, freq=1000, amp=0.3):
    t = np.arange(int(seconds * FS)) / FS
    return amp * np.sin(2 * np.pi * freq * t)


def voice(seconds=15, amp=0.3, seed=5):
    """Crude synthetic speech: gliding pitch, moving formants, syllables, pauses."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    t = np.arange(n) / FS
    # pitch glides 100..180 Hz
    pitch = 130 + 30 * np.sin(2 * np.pi * 0.3 * t) + 15 * np.sin(2 * np.pi * 1.7 * t + 1)
    phase = 2 * np.pi * np.cumsum(pitch) / FS
    sig = np.zeros(n)
    # formants change every syllable (~180 ms)
    syl = int(0.18 * FS)
    f1 = np.repeat(rng.uniform(300, 800, n // syl + 1), syl)[:n]
    f2 = np.repeat(rng.uniform(900, 2300, n // syl + 1), syl)[:n]
    f1 = np.convolve(f1, np.ones(400) / 400, mode="same")
    f2 = np.convolve(f2, np.ones(400) / 400, mode="same")
    for h in range(1, 25):
        fh = h * pitch
        g = np.exp(-((fh - f1) / 150) ** 2) + 0.6 * np.exp(-((fh - f2) / 200) ** 2) + 0.05
        g = g * (fh > 250) * (fh < 2800)
        sig += g * np.sin(h * phase)
    # syllable gate with pauses between words
    gate = np.zeros(n)
    pos = 0
    while pos < n:
        length = int(rng.uniform(0.1, 0.3) * FS)
        gate[pos:pos + length] = rng.uniform(0.5, 1.0)
        pos += length + int(rng.uniform(0.03, 0.12) * FS)
        if rng.random() < 0.15:
            pos += int(rng.uniform(0.3, 0.8) * FS)
    gate = np.convolve(gate, np.hanning(300) / np.hanning(300).sum(), mode="same")
    sig = sig * gate
    return amp * sig / (np.abs(sig).max() + 1e-9)
