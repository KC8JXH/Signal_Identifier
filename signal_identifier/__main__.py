"""Run with:  python -m signal_identifier            (opens the window)
           python -m signal_identifier --demo     (window with made-up signals)
           python -m signal_identifier file.wav   (identify signals in a recording)
"""

from __future__ import annotations

import argparse
import sys
import wave

import numpy as np

from .classify import analyze


def read_wav(path: str) -> tuple[np.ndarray, float]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float64) / 2**31
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) / 128
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)
        v = np.where(v >= 2**23, v - 2**24, v)
        x = v.astype(np.float64) / 2**23
    else:
        raise ValueError(f"unsupported WAV sample size: {width} bytes")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, float(rate)


def identify_file(path: str, chunk: float = 15.0) -> None:
    x, rate = read_wav(path)
    n = int(chunk * rate)
    for start in range(0, max(1, len(x)), n):
        seg = x[start:start + n]
        if len(seg) < 3 * rate:
            break
        res = analyze(seg, rate, keep_spectrogram=False)
        t0 = start / rate
        found = "; ".join(d.describe() for d in res.detections) or "nothing (just noise)"
        print(f"{int(t0 // 60):02d}:{t0 % 60:04.1f}  {found}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="signal_identifier",
                                description="Identify ham radio signal modes from receiver audio.")
    p.add_argument("wav", nargs="?", help="a WAV recording to analyse (leave out to open the window)")
    p.add_argument("--demo", action="store_true", help="open the window with made-up signals")
    a = p.parse_args(argv)
    if a.wav:
        identify_file(a.wav)
        return 0
    from .app import main as gui
    gui(demo=a.demo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
