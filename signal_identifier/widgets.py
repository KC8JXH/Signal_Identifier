"""Custom display widgets: an analog S-meter and a scrolling waterfall."""

from __future__ import annotations

import math
import tkinter as tk

import numpy as np

from .dsp import decimate

# ---------------------------------------------------------------- S-meter


def meter_fraction(db: float | None) -> float:
    """dB relative to S9 -> needle position 0..1 (S0..S9 on the first 60%)."""
    if db is None:
        return 0.0
    if db <= 0:
        return max(0.0, (9 + db / 6) / 9 * 0.6)
    return 0.6 + min(db, 60) / 60 * 0.4


class SMeter(tk.Canvas):
    """Analog S-meter with a needle that swings like a real one."""

    W, H = 320, 170
    A0, A1 = 145.0, 35.0   # needle sweep, degrees (left to right)

    def __init__(self, master, **kw):
        super().__init__(master, width=self.W, height=self.H, bg="#1b1b1b",
                         highlightthickness=0, **kw)
        self.target = None
        self.pos = 0.0
        self.cx, self.cy, self.r = self.W / 2, self.H + 40, 175
        self._draw_face()
        self.needle = self.create_line(0, 0, 0, 0, width=2, fill="#c00000", capstyle="round")
        self.readout = self.create_text(self.W - 12, self.H - 14, anchor="e", text="",
                                        font=("Segoe UI", 11, "bold"), fill="#222")
        self.note = self.create_text(12, self.H - 14, anchor="w", text="No radio",
                                     font=("Segoe UI", 9), fill="#666")
        self._place_needle()
        self.after(50, self._animate)

    def _xy(self, frac: float, r: float):
        a = math.radians(self.A0 + (self.A1 - self.A0) * frac)
        return self.cx + r * math.cos(a), self.cy - r * math.sin(a)

    def _arc(self, f0, f1, r, colour, width):
        pts = []
        for i in range(31):
            pts += self._xy(f0 + (f1 - f0) * i / 30, r)
        self.create_line(*pts, fill=colour, width=width, smooth=True)

    def _draw_face(self):
        self.create_rectangle(4, 4, self.W - 4, self.H - 4, fill="#f4ecd6", outline="#888", width=2)
        self.create_text(14, 14, anchor="nw", text="S", font=("Georgia", 14, "bold italic"), fill="#333")
        self._arc(0, 0.6, self.r - 40, "#222", 2)
        self._arc(0.6, 1.0, self.r - 40, "#c00000", 3)
        marks = [(f"{n}", meter_fraction((n - 9) * 6), "#222") for n in (1, 3, 5, 7, 9)]
        marks += [(f"+{d}", meter_fraction(d), "#c00000") for d in (20, 40, 60)]
        for n in range(0, 10):
            f = meter_fraction((n - 9) * 6)
            x0, y0 = self._xy(f, self.r - 40)
            x1, y1 = self._xy(f, self.r - (52 if n % 2 else 46))
            self.create_line(x0, y0, x1, y1, fill="#222", width=1)
        for d in (10, 20, 30, 40, 50, 60):
            f = meter_fraction(d)
            x0, y0 = self._xy(f, self.r - 40)
            x1, y1 = self._xy(f, self.r - (52 if d % 20 == 0 else 46))
            self.create_line(x0, y0, x1, y1, fill="#c00000", width=1)
        for text, f, colour in marks:
            x, y = self._xy(f, self.r - 64)
            self.create_text(x, y, text=text, font=("Segoe UI", 10, "bold"), fill=colour)

    def set(self, db: float | None, connected: bool):
        self.target = db
        if not connected:
            self.itemconfig(self.note, text="No radio")
            self.itemconfig(self.readout, text="")
        elif db is None:
            self.itemconfig(self.note, text="Meter not available")
            self.itemconfig(self.readout, text="")
        else:
            from .radio import s_units_text
            self.itemconfig(self.note, text="")
            self.itemconfig(self.readout, text=s_units_text(db))

    def _place_needle(self):
        x0, y0 = self._xy(self.pos, 30)
        x1, y1 = self._xy(self.pos, self.r - 38)
        self.coords(self.needle, x0, y0, x1, y1)

    def _animate(self):
        goal = meter_fraction(self.target)
        # Quick to rise, slower to fall, like a real meter.
        k = 0.55 if goal > self.pos else 0.18
        self.pos += (goal - self.pos) * k
        self._place_needle()
        self.after(50, self._animate)


# ---------------------------------------------------------------- waterfall


def colormap(v: np.ndarray) -> np.ndarray:
    """0..1 -> RGB (black -> blue -> cyan -> yellow -> red -> white), rig style."""
    stops = np.array([[0, 0, 20], [0, 30, 130], [0, 160, 230], [240, 240, 40],
                      [250, 90, 20], [255, 255, 255]], float)
    pos = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


class Waterfall(tk.Canvas):
    """Scrolling high-resolution waterfall (newest at the top), like a rig's display.

    Uses a 4096-point FFT (~3 Hz per line at 12 kHz), so the individual tones
    of FT8 and the two tones of RTTY show up as separate lines.
    """

    AXIS_H = 56
    NFFT = 4096
    RANGE_DB = 32.0

    def __init__(self, master, fmax: float, **kw):
        super().__init__(master, bg="#000014", highlightthickness=0, width=10, height=10, **kw)
        self.fmax = fmax
        self.buf = None
        self.img = None
        self.ref = None
        self.dial = None      # (dial_hz, mode, pitch) when the radio is connected
        self.markers = []
        self.bind("<Configure>", lambda e: self._resize())

    # -- geometry --------------------------------------------------------------

    def _size(self):
        return max(100, self.winfo_width()), max(self.AXIS_H + 40, self.winfo_height())

    def _resize(self):
        w, h = self._size()
        rows = h - self.AXIS_H
        if self.buf is None or self.buf.shape[1] != w:
            self.buf = np.zeros((rows, w, 3), np.uint8)
            self.buf[:] = colormap(np.zeros(1))[0]
        elif self.buf.shape[0] != rows:
            new = np.zeros((rows, w, 3), np.uint8)
            new[:] = colormap(np.zeros(1))[0]
            n = min(rows, self.buf.shape[0])
            new[:n] = self.buf[:n]
            self.buf = new
        self._draw_axis()
        self._render()

    def clear(self):
        self.buf = None
        self.markers = []
        self._resize()

    # -- data ------------------------------------------------------------------

    def need_samples(self, rate: float) -> int:
        factor = max(1, int(round(rate / 12000)))
        return self.NFFT * factor + 256

    def add_audio(self, audio: np.ndarray, rate: float):
        """Add one new line to the waterfall from the most recent audio."""
        if self.buf is None:
            self._resize()
        x, fs = decimate(audio, rate)
        if len(x) < self.NFFT:
            return
        seg = x[-self.NFFT:] * np.hanning(self.NFFT)
        p = 10 * np.log10(np.abs(np.fft.rfft(seg)) ** 2 + 1e-20)
        freqs = np.fft.rfftfreq(self.NFFT, 1 / fs)
        w = self.buf.shape[1]
        edges = np.linspace(0, self.fmax, w + 1)
        idx = np.clip(np.searchsorted(freqs, edges[:-1]), 0, len(p) - 1)
        stop = int(np.searchsorted(freqs, self.fmax)) + 1
        row = np.maximum.reduceat(p[:stop], np.minimum(idx, stop - 1))[:w]
        # Reference = the background level; follows AGC so the picture stays steady.
        ref_now = np.percentile(row, 40)
        self.ref = ref_now if self.ref is None else 0.8 * self.ref + 0.2 * ref_now
        v = (row - self.ref + 2) / self.RANGE_DB
        line = colormap(v)
        self.buf[1:] = self.buf[:-1]
        self.buf[0] = line
        self._render()

    def _render(self):
        if self.buf is None:
            return
        h, w, _ = self.buf.shape
        ppm = f"P6 {w} {h} 255 ".encode() + self.buf.tobytes()
        img = tk.PhotoImage(data=ppm, format="PPM")
        if self.img is None or not self.find_withtag("wf"):
            self.create_image(0, self.AXIS_H, image=img, anchor="nw", tags="wf")
        else:
            self.itemconfig("wf", image=img)
        self.img = img  # keep a reference so Tk doesn't discard it

    # -- scale and labels --------------------------------------------------------

    def set_dial(self, dial):
        if dial != self.dial:
            self.dial = dial
            self._draw_axis()

    def _draw_axis(self):
        from .radio import format_freq, rf_frequency
        self.delete("axis")
        w, _ = self._size()
        self.create_rectangle(0, 0, w, self.AXIS_H, fill="#0b0f14", outline="", tags="axis")
        step = 100
        for f in range(0, int(self.fmax) + 1, step):
            x = f / self.fmax * w
            major = f % 500 == 0
            self.create_line(x, self.AXIS_H - (9 if major else 4), x, self.AXIS_H,
                             fill="#9aa" if major else "#566", tags="axis")
            if not major:
                continue
            # keep the end labels fully on screen
            anchor = "w" if x < 40 else "e" if x > w - 40 else "center"
            xx = x + 3 if anchor == "w" else x - 3 if anchor == "e" else x
            rf = rf_frequency(*self.dial[:2], f, self.dial[2]) if self.dial else None
            if rf:
                self.create_text(xx, 10, text=format_freq(rf), fill="#e8e8e8", anchor=anchor,
                                 font=("Consolas", 10, "bold"), tags="axis")
                self.create_text(xx, 24, text=f"{f} Hz", fill="#7f8c99", anchor=anchor,
                                 font=("Segoe UI", 8), tags="axis")
            else:
                self.create_text(xx, 12, text=f"{f} Hz", fill="#cfd8e0", anchor=anchor,
                                 font=("Segoe UI", 9), tags="axis")
        self._draw_markers()

    def set_markers(self, markers):
        """markers: list of (audio_hz, text, colour)."""
        self.markers = markers
        self._draw_markers()

    def _draw_markers(self):
        self.delete("marker")
        w, _ = self._size()
        for f, text, colour in self.markers:
            x = f / self.fmax * w
            self.create_text(x, self.AXIS_H - 16, text=text, fill=colour,
                             font=("Segoe UI", 10, "bold"), tags="marker")
            self.create_line(x, self.AXIS_H - 8, x, self.AXIS_H, fill=colour, width=2, tags="marker")
