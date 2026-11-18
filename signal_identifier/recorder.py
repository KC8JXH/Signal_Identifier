"""Record receiver audio to a WAV file for as long as you like."""

from __future__ import annotations

import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np


class Recorder:
    """Writes audio to a 16-bit mono WAV in the background while it's fed."""

    def __init__(self, path: Path, rate: float):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rate = int(round(rate))
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self.rate)
        self._q: queue.Queue = queue.Queue()
        self.samples = 0
        self.started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, x: np.ndarray):
        """Called with each new chunk of audio (from the sound-card thread)."""
        self._q.put(np.array(x, dtype=np.float32, copy=True))

    def _run(self):
        while True:
            x = self._q.get()
            if x is None:
                break
            pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
            self._wav.writeframes(pcm.tobytes())
            self.samples += len(pcm)

    @property
    def seconds(self) -> float:
        return self.samples / self.rate

    def stop(self) -> Path:
        self._q.put(None)
        self._thread.join(timeout=5)
        self._wav.close()
        return self.path


def elapsed_text(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"
