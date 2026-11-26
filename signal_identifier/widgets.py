"""Custom display widgets: the scrolling audio waterfall and the whole-band view."""

from __future__ import annotations

import tkinter as tk

import numpy as np

from .dsp import decimate

# ---------------------------------------------------------------- meter scale


def meter_fraction(db: float | None) -> float:
    """dB relative to S9 -> 0..1 for a bar meter (S0..S9 on the first 60%)."""
    if db is None:
        return 0.0
    if db <= 0:
        return max(0.0, (9 + db / 6) / 9 * 0.6)
    return 0.6 + min(db, 60) / 60 * 0.4


def swr_fraction(swr: float | None) -> float:
    """SWR -> 0..1 on an Icom-style scale: 1.0, 1.5 at 1/4, 2 at 1/2, 3 at 3/4."""
    if swr is None:
        return 0.0
    if swr <= 1.5:
        return max(0.0, (swr - 1) / 2)
    if swr <= 2:
        return 0.25 + (swr - 1.5) / 2
    if swr <= 3:
        return 0.5 + (swr - 2) / 4
    return min(1.0, 0.75 + (swr - 3) / 28)


class MeterBar(tk.Canvas):
    """Small bar meter with a shaded danger zone and a peak-hold tick."""

    def __init__(self, master, width=130, zone_from=None, **kw):
        super().__init__(master, width=width, height=14, bg="#1b222b", highlightthickness=1,
                         highlightbackground="#5c6773", **kw)
        self.w = width
        self.zone_from = zone_from
        self.pos = 0.0
        self.peak, self.peak_t = 0.0, 0.0
        if zone_from is not None:
            self.create_rectangle(zone_from * width, 0, width + 2, 16, fill="#4a2326", width=0)
        self.bar = self.create_rectangle(0, 0, 0, 16, fill="#37b24d", width=0)
        self.tick = self.create_line(0, 0, 0, 16, fill="#f1f3f5", width=2, state="hidden")

    def set(self, frac: float | None, color: str = "#37b24d"):
        """frac 0..1, or None to show the meter as idle."""
        import time
        now = time.monotonic()
        if frac is None:
            self.pos = 0.0
            self.coords(self.bar, 0, 0, 0, 16)
            self.itemconfigure(self.tick, state="hidden")
            return
        goal = max(0.0, min(1.0, frac))
        self.pos += (goal - self.pos) * (0.7 if goal > self.pos else 0.3)   # meter-like ballistics
        if goal >= self.peak or now - self.peak_t > 1.5:
            self.peak, self.peak_t = goal, now
        self.coords(self.bar, 0, 0, self.pos * self.w, 16)
        self.itemconfigure(self.bar, fill=color)
        x = max(1, self.peak * self.w)
        self.coords(self.tick, x, 0, x, 16)
        self.itemconfigure(self.tick, state="normal")


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

    def __init__(self, master, fmax: float, on_click=None, **kw):
        super().__init__(master, bg="#000014", highlightthickness=0, width=10, height=10, **kw)
        self.fmax = fmax
        self.on_click = on_click   # called with the audio frequency (Hz) clicked
        self.hover = None
        self.message = ""
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._set_hover(e.x))
        self.bind("<Leave>", lambda e: self._set_hover(None))
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

    def set_message(self, text: str):
        """Show a note across the waterfall (e.g. when not listening)."""
        if text == self.message and self.find_withtag("msg"):
            return
        self.message = text
        self.delete("msg")
        if text:
            w, h = self._size()
            self.create_text(w / 2, self.AXIS_H + (h - self.AXIS_H) / 2, text=text, fill="#9aa5b1",
                             font=("Segoe UI", 14), tags="msg")

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
        if self.message:
            self.set_message("")
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

    def _set_hover(self, x):
        self.hover = x
        self._draw_hover()

    def _draw_hover(self):
        from .radio import format_freq, rf_frequency
        self.delete("hover")
        if self.hover is None:
            return
        w, h = self._size()
        f = self.hover / w * self.fmax
        rf = rf_frequency(*self.dial[:2], f, self.dial[2]) if self.dial else None
        text = (format_freq(rf) if rf else f"{f:.0f} Hz") + (" - click to tune" if self.on_click and rf else "")
        self.create_line(self.hover, self.AXIS_H, self.hover, h, fill="#ffff66", dash=(2, 3), tags="hover")
        anchor = "w" if self.hover < w - 180 else "e"
        self.create_text(self.hover + (6 if anchor == "w" else -6), self.AXIS_H + 12, text=text,
                         fill="#ffff66", anchor=anchor, font=("Segoe UI", 9, "bold"), tags="hover")

    def _click(self, e):
        if self.on_click is not None:
            w, _ = self._size()
            self.on_click(e.x / w * self.fmax)

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


# ---------------------------------------------------------------- whole-band view


def _mhz3(hz: float) -> str:
    return f"{hz / 1e6:.3f}"


class BandScope(tk.Canvas):
    """Whole-band waterfall from the radio's own band scope (Icom CI-V).

    Shows band-plan hints along the top, a box around the slice you are
    listening to, and (optionally) tunes the radio where you click.
    """

    AXIS_H = 20
    HINT_H = 16
    HEIGHT = 170

    def __init__(self, master, on_click=None, **kw):
        super().__init__(master, bg="#000014", highlightthickness=0, width=10, height=self.HEIGHT, **kw)
        self.on_click = on_click
        self.buf = None
        self.img = None
        self.view = None          # (lo_hz, hi_hz) shown across the width
        self.last_sweep = None
        self.state = None         # RadioState
        self.message = "Connect an Icom radio (like the IC-7300) to see the whole band here."
        self.no_signal = False    # radio connected but no scope data arriving
        self.hint = ""            # small text under NO SIGNAL
        self.hover = None
        self.bind("<Configure>", lambda e: self._reset())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._motion(None))
        self._reset()

    @property
    def top(self):
        return self.AXIS_H + self.HINT_H

    def _size(self):
        return max(100, self.winfo_width()), max(self.top + 20, self.winfo_height())

    def _reset(self):
        w, h = self._size()
        self.buf = np.zeros((h - self.top, w, 3), np.uint8)
        self.buf[:] = colormap(np.zeros(1))[0]
        self._render()
        self._overlay()

    def x_to_hz(self, x: float) -> float | None:
        if not self.view:
            return None
        w, _ = self._size()
        lo, hi = self.view
        return lo + (hi - lo) * x / w

    def hz_to_x(self, hz: float) -> float:
        w, _ = self._size()
        lo, hi = self.view
        return (hz - lo) / (hi - lo) * w

    # -- data -------------------------------------------------------------------

    def update_scope(self, sweep, state):
        """Called regularly by the window with the newest sweep and radio state."""
        self.state = state
        if sweep is None:
            if self.last_sweep is not None and not self.no_signal:
                self.show_no_signal()          # data was flowing and has stopped
            elif self.view is None:
                self._overlay()
            return
        if sweep is self.last_sweep:
            return
        self.last_sweep = sweep
        self.no_signal = False
        self.hint = ""
        from .bandplan import band_for
        band = band_for((sweep.lo_hz + sweep.hi_hz) / 2) or band_for(state.freq_hz if state else None)
        if band and sweep.lo_hz <= band[0] and sweep.hi_hz >= band[1]:
            view = (float(band[0]), float(band[1]))   # scope covers the whole band: show just the band
        else:
            view = (sweep.lo_hz, sweep.hi_hz)
        if view != self.view:
            self.view = view
            self._reset()
        w = self.buf.shape[1]
        vals = np.asarray(sweep.values, float)
        cols = self.view[0] + (np.arange(w) + 0.5) / w * (self.view[1] - self.view[0])
        pos = (cols - sweep.lo_hz) / (sweep.hi_hz - sweep.lo_hz) * (len(vals) - 1)
        row = np.interp(pos, np.arange(len(vals)), vals, left=0, right=0)
        floor = np.percentile(vals, 25)
        line = colormap((row - floor + 3) / 70.0)
        self.buf[1:] = self.buf[:-1]
        self.buf[0] = line
        self.message = ""
        self._render()
        self._overlay()

    def set_message(self, text: str):
        if text == self.message and not self.no_signal:
            return
        self.message = text
        self.no_signal = False
        self._overlay()

    def show_no_signal(self, hint: str = ""):
        """Big white NO SIGNAL in the middle of the scope, with an optional hint under it."""
        if self.no_signal and hint == self.hint:
            return
        self.no_signal = True
        self.hint = hint
        self._overlay()

    def _draw_no_signal(self, w, h):
        cx, cy = w / 2, self.top + (h - self.top) / 2 - (8 if self.hint else 0)
        font = ("Segoe UI", 26, "bold")
        for dx, dy in ((2, 2), (-1, -1), (2, -1), (-1, 2)):   # dark outline so it reads on any colour
            self.create_text(cx + dx, cy + dy, text="NO SIGNAL", fill="#000000", font=font, tags="ov")
        self.create_text(cx, cy, text="NO SIGNAL", fill="#ffffff", font=font, tags="ov")
        if self.hint:
            self.create_text(cx, cy + 30, text=self.hint, fill="#c9d1d9", font=("Segoe UI", 9),
                             width=w - 40, justify="center", tags="ov")

    def _render(self):
        h, w, _ = self.buf.shape
        img = tk.PhotoImage(data=f"P6 {w} {h} 255 ".encode() + self.buf.tobytes(), format="PPM")
        if not self.find_withtag("img"):
            self.create_image(0, self.top, image=img, anchor="nw", tags="img")
        else:
            self.itemconfig("img", image=img)
        self.img = img

    # -- overlay: scale, band plan, listening box ----------------------------

    def _overlay(self):
        from .bandplan import COLOURS, segments_between
        from .radio import rf_frequency
        self.delete("ov")
        w, h = self._size()
        self.create_rectangle(0, 0, w, self.top, fill="#0b0f14", outline="", tags="ov")
        if not self.view:
            if self.no_signal:
                self._draw_no_signal(w, h)
            else:
                self.create_text(w / 2, h / 2, text=self.message, fill="#9aa5b1",
                                 font=("Segoe UI", 11), tags="ov")
            return
        lo, hi = self.view
        span = hi - lo
        step = next(s for s in (1e3, 2e3, 5e3, 10e3, 25e3, 50e3, 100e3, 250e3, 1e6) if span / s <= 14)
        f = np.ceil(lo / step) * step
        while f <= hi:
            x = self.hz_to_x(f)
            self.create_line(x, self.AXIS_H - 5, x, self.AXIS_H, fill="#9aa", tags="ov")
            anchor = "w" if x < 30 else "e" if x > w - 30 else "center"
            self.create_text(x, 9, text=_mhz3(f), fill="#cfd8e0", anchor=anchor,
                             font=("Consolas", 9), tags="ov")
            f += step
        # band-plan hints
        label_end = -1e9
        for s_lo, s_hi, label in sorted(segments_between(lo, hi), key=lambda t: t[1] - t[0], reverse=True):
            x0, x1 = max(0, self.hz_to_x(s_lo)), min(w, self.hz_to_x(s_hi))
            colour = COLOURS.get(label, "#868e96")
            self.create_rectangle(x0, self.AXIS_H + 2, x1, self.top - 2, fill=colour, outline="", tags="ov")
            if x1 - x0 > len(label) * 7:
                self.create_text((x0 + x1) / 2, self.AXIS_H + self.HINT_H / 2, text=label, fill="white",
                                 font=("Segoe UI", 8, "bold"), tags="ov")
            elif label in ("FT8", "FT4") and x0 > label_end:
                # narrow slot: small label just above the waterfall, without overlapping
                self.create_text(x0, self.top + 2, text=label, fill="#b2f2bb", anchor="nw",
                                 font=("Segoe UI", 8, "bold"), tags="ov")
                label_end = x0 + 26
        # the slice you're listening to
        st = self.state
        if st and st.ok and st.freq_hz:
            a = rf_frequency(st.freq_hz, st.mode, 0, st.cw_pitch)
            b = rf_frequency(st.freq_hz, st.mode, 3500, st.cw_pitch)
            if a is not None and b is not None:
                x0, x1 = sorted((self.hz_to_x(a), self.hz_to_x(b)))
                x1 = max(x1, x0 + 2)
                self.create_rectangle(x0, self.top, x1, h - 1, outline="#ffffff", width=1,
                                      dash=(3, 2), tags="ov")
            xd = self.hz_to_x(st.freq_hz)
            self.create_line(xd, self.top, xd, h, fill="#ff4040", width=1, tags="ov")
        if self.hover is not None:
            hz = self.x_to_hz(self.hover)
            self.create_line(self.hover, self.top, self.hover, h, fill="#ffff66", dash=(2, 3), tags="ov")
            tip = _mhz3(hz) + (" - click to bring into view" if self.on_click else "")
            anchor = "w" if self.hover < w - 160 else "e"
            self.create_text(self.hover + (6 if anchor == "w" else -6), self.top + 10, text=tip,
                             fill="#ffff66", anchor=anchor, font=("Segoe UI", 9, "bold"), tags="ov")
        if self.message:
            self.create_text(w / 2, h - 14, text=self.message, fill="#ffa94d", font=("Segoe UI", 9), tags="ov")
        if self.no_signal:
            self._draw_no_signal(w, h)

    def _motion(self, e):
        self.hover = None if (e is None or not self.view) else e.x
        self._overlay()

    def _click(self, e):
        hz = self.x_to_hz(e.x)
        if hz is not None and self.on_click is not None:
            self.on_click(hz)
