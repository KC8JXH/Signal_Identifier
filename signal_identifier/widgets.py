"""Custom display widgets: the radio-style scope (spectrum + waterfall) and meters."""

from __future__ import annotations

import tkinter as tk

import numpy as np


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


# ---------------------------------------------------------------- scrolling picture


class ScrollingImage:
    """A picture that gains one new line at the top and scrolls down.

    Rebuilding the whole picture for every line (a few MB each time) kept the
    window too busy to answer menus and clicks. Instead the picture is a ring:
    each new line is written into one row of a Tk photo image, and the image
    is shown twice, one copy above the other, moved so that the newest row is
    at the top. Nothing is copied; only one row is written per line."""

    def __init__(self, canvas: tk.Canvas, x: int, y: int, tag: str):
        self.canvas, self.x, self.y, self.tag = canvas, x, y, tag
        self.img = None
        self.w = self.h = 0
        self.top = 0          # image row currently shown at the top

    def load(self, buf: np.ndarray):
        """Show a whole picture (rows x width x RGB), e.g. after a resize."""
        h, w, _ = buf.shape
        ppm = f"P6 {w} {h} 255 ".encode() + np.ascontiguousarray(buf).tobytes()
        self.img = tk.PhotoImage(data=ppm, format="PPM")
        self.w, self.h, self.top = w, h, 0
        self.canvas.delete(self.tag)
        self.canvas.create_image(self.x, self.y, image=self.img, anchor="nw", tags=(self.tag, self.tag + "1"))
        self.canvas.create_image(self.x, self.y + h, image=self.img, anchor="nw", tags=(self.tag, self.tag + "2"))
        self.canvas.tag_lower(self.tag)      # scales, labels etc. stay on top

    def push(self, line: np.ndarray):
        """Add one line (width x RGB) at the top; everything else moves down one row."""
        if self.img is None or len(line) != self.w:
            return False
        self.top = (self.top - 1) % self.h
        row = tk.PhotoImage(data=f"P6 {self.w} 1 255 ".encode() + np.ascontiguousarray(line).tobytes(),
                            format="PPM")
        self.img.tk.call(self.img, "copy", row, "-to", 0, self.top)
        self.canvas.coords(self.tag + "1", self.x, self.y - self.top)
        self.canvas.coords(self.tag + "2", self.x, self.y - self.top + self.h)
        return True


class AnalogMeter(tk.Canvas):
    """Needle meter like the radio's: an S-meter on receive that turns into a
    power meter (watts) while transmitting."""

    W, H = 176, 104

    def __init__(self, master, **kw):
        super().__init__(master, width=self.W, height=self.H, bg="#0b0f14", highlightthickness=1,
                         highlightbackground="#2a3340", **kw)
        self.cx, self.cy, self.r = self.W / 2, self.H - 14, self.H - 30
        self.kind = None          # "S" or "PO"
        self.max_w = 100.0
        self.pos = 0.0
        self._scale("S")
        self.needle = self.create_line(self.cx, self.cy, self.cx, self.cy - self.r, fill="#ffa94d", width=2)
        self.create_oval(self.cx - 4, self.cy - 4, self.cx + 4, self.cy + 4, fill="#ffa94d", outline="")
        self.readout = self.create_text(self.W - 6, self.H - 6, text="", anchor="se", fill="#e9ecef",
                                        font=("Consolas", 9, "bold"))
        self.tx = False
        self.tx_label = self.create_text(7, 9, text="", anchor="w", fill="#ffffff", font=("Segoe UI", 8, "bold"))

    def _angle(self, frac):
        """0..1 across the scale -> angle in degrees (150 at the left, 30 at the right)."""
        import math
        return math.radians(150 - 120 * max(0.0, min(1.0, frac)))

    def _point(self, frac, r):
        import math
        a = self._angle(frac)
        return self.cx + r * math.cos(a), self.cy - r * math.sin(a)

    def _scale(self, kind, max_w=100.0):
        if kind == self.kind and max_w == self.max_w:
            return
        self.kind, self.max_w = kind, max_w
        self.delete("scale")
        r = self.r
        if kind == "S":
            # S0..S9 over the first 60 %, then +20 / +40 / +60 dB (red), like meter_fraction()
            marks = [(meter_fraction((n - 9) * 6), str(n), "#e9ecef") for n in (1, 3, 5, 7, 9)]
            marks += [(meter_fraction(db), f"+{db}", "#ff6b6b") for db in (20, 40, 60)]
            red_from = 0.6
            title = "S"
        else:
            marks = [(f, f"{f * max_w:g}", "#e9ecef") for f in (0, 0.25, 0.5, 0.75, 1.0)]
            red_from = None
            title = "Po W"
        steps = 40
        for k in range(steps):                                   # the arc
            f0, f1 = k / steps, (k + 1) / steps
            colour = "#ff6b6b" if red_from is not None and f0 >= red_from else "#adb5bd"
            self.create_line(*self._point(f0, r), *self._point(f1, r), fill=colour, width=2, tags="scale")
        for f, label, colour in marks:
            self.create_line(*self._point(f, r - 6), *self._point(f, r + 2), fill=colour, width=2, tags="scale")
            x, y = self._point(f, r + 11)
            self.create_text(x, y, text=label, fill=colour, font=("Segoe UI", 7, "bold"), tags="scale")
        self.create_text(8, self.H - 6, text=title, anchor="sw", fill="#39c5ff",
                         font=("Segoe UI", 8, "bold"), tags="scale")
        self.tag_lower("scale")

    def _set_tx(self, on: bool):
        """Transmitting: the whole meter turns red (red frame and face, TX label)."""
        if on == self.tx:
            return
        self.tx = on
        self.config(bg="#3a0d0d" if on else "#0b0f14",
                    highlightbackground="#ff3b3b" if on else "#2a3340", highlightthickness=3 if on else 1)
        self.itemconfig(self.tx_label, text="TX" if on else "")

    def set_s(self, db, text):
        """Receive: S-meter (dB relative to S9), e.g. text 'S7'."""
        self._set_tx(False)
        self._scale("S")
        self._move(meter_fraction(db) if db is not None else 0.0, text or "")

    def set_power(self, watts, max_w, text):
        """Transmit: output power in watts (and the meter turns red)."""
        self._set_tx(True)
        self._scale("PO", float(max_w or 100))
        self._move((watts or 0.0) / (max_w or 100), text or "")

    def _move(self, goal, text):
        goal = max(0.0, min(1.0, goal))
        self.pos += (goal - self.pos) * (0.6 if goal > self.pos else 0.25)   # needle ballistics
        self.coords(self.needle, self.cx, self.cy, *self._point(self.pos, self.r - 4))
        self.itemconfig(self.readout, text=text)


# ---------------------------------------------------------------- colours


def colormap(v: np.ndarray) -> np.ndarray:
    """0..1 -> RGB (black -> blue -> cyan -> yellow -> red -> white), rig style."""
    stops = np.array([[0, 0, 20], [0, 30, 130], [0, 160, 230], [240, 240, 40],
                      [250, 90, 20], [255, 255, 255]], float)
    pos = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


# ---------------------------------------------------------------- whole-band view


def _mhz3(hz: float) -> str:
    return f"{hz / 1e6:.3f}"


class BandScope(tk.Canvas):
    """The radio's own scope (Icom CI-V), laid out like the IC-7300's screen:
    a spectrum (signal strength across the span) on top, the waterfall below.

    The receive frequency is marked, with its passband shaded; identified
    signals are labelled at their frequency; clicking tunes the radio there.
    """

    AXIS_H = 18          # frequency scale
    LABEL_H = 18         # strip for identification labels
    SPEC_FRAC = 0.18     # share of the scope's height used by the spectrum
    SCALE_MAX = 160.0    # Icom scope values run 0..160
    SPEC_BG = "#030b1a"
    GRID = "#28405e"

    def __init__(self, master, on_click=None, **kw):
        super().__init__(master, bg="#000014", highlightthickness=0, width=10, height=300, **kw)
        self.on_click = on_click
        self.buf = None
        self.img = None
        self.view = None          # (lo_hz, hi_hz) shown across the width
        self.last_sweep = None
        self.state = None         # RadioState
        self.message = "Connect the radio (Settings > Connect radio) to see its scope here."
        self.no_signal = False    # radio connected but no scope data arriving
        self.hint = ""            # small text under NO SIGNAL
        self.hover = None
        self.markers = []         # (rf_hz, text, colour) from the identifier
        self.pic = ScrollingImage(self, 0, 0, "img")
        self.bind("<Configure>", lambda e: self._reset())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._motion(None))

    # -- geometry ------------------------------------------------------------------

    def _size(self):
        return max(100, self.winfo_width()), max(160, self.winfo_height())

    def _geom(self):
        w, h = self._size()
        spec_top = self.AXIS_H + self.LABEL_H
        spec_h = max(50, int((h - spec_top) * self.SPEC_FRAC))
        wf_top = spec_top + spec_h + 2
        return w, h, spec_top, spec_h, wf_top

    @property
    def top(self):
        return self._geom()[4]

    def span_half(self):
        return (self.view[1] - self.view[0]) / 2 if self.view else None

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

    def _reset(self):
        w, h, spec_top, spec_h, wf_top = self._geom()
        self.buf = np.zeros((h - wf_top, w, 3), np.uint8)
        self.buf[:] = colormap(np.zeros(1))[0]
        self.pic.y = wf_top
        self.pic.load(self.buf)
        self._static()
        self._overlay()

    # -- data ----------------------------------------------------------------------

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
        view = (float(sweep.lo_hz), float(sweep.hi_hz))   # exactly what the radio's scope shows
        if view != self.view:
            old = self.view
            self.view = view
            if old and abs((old[1] - old[0]) - (view[1] - view[0])) < 1 and self.buf is not None:
                self._slide(old[0], view[0])   # tuned, same span: slide the picture, like the radio
            else:
                self._reset()                  # new span: start the waterfall afresh, like the radio
        w, h, spec_top, spec_h, wf_top = self._geom()
        vals = np.asarray(sweep.values, float)
        pos = (np.arange(w) + 0.5) / w * (len(vals) - 1)
        row = np.interp(pos, np.arange(len(vals)), vals)
        # spectrum: filled trace on the radio's own fixed scale (0..160), so the
        # RF gain and the radio's REF level change it just as on the radio
        bottom = spec_top + spec_h
        ys = bottom - np.clip(row / self.SCALE_MAX, 0, 1) * (spec_h - 2)
        step = 2 if w > 900 else 1
        xs = np.arange(0, w, step)
        pts = np.empty(2 * len(xs) + 4)
        pts[0:2] = (0, bottom)
        pts[2:-2:2] = xs
        pts[3:-2:2] = ys[::step]
        pts[-2:] = (w, bottom)
        self.coords("spec", *pts.tolist())
        self.coords("specline", *pts[2:-2].tolist())
        # waterfall: one new line on top
        floor = np.percentile(vals, 25)
        line = colormap((row - floor + 3) / 70.0)
        self.buf[1:] = self.buf[:-1]
        self.buf[0] = line
        self.message = ""
        if not self.pic.push(line):
            self.pic.load(self.buf)
        self._overlay()

    def _slide(self, old_lo: float, new_lo: float):
        """The radio was tuned but the span is the same: move the waterfall
        sideways so its history stays lined up with the frequencies."""
        w = self.buf.shape[1]
        span = self.view[1] - self.view[0]
        dx = int(round((new_lo - old_lo) / span * w))
        bg = colormap(np.zeros(1))[0]
        if abs(dx) >= w:
            self.buf[:] = bg
        elif dx > 0:                        # tuned up: picture moves left
            self.buf[:, :-dx] = self.buf[:, dx:]
            self.buf[:, -dx:] = bg
        elif dx < 0:
            self.buf[:, -dx:] = self.buf[:, :dx]
            self.buf[:, :-dx] = bg
        self.pic.load(self.buf)
        self._static()

    def set_markers(self, markers):
        """markers: list of (rf_hz, text, colour) - identified signals."""
        if markers != self.markers:
            self.markers = markers
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
        cx, cy = w / 2, self.AXIS_H + (h - self.AXIS_H) / 2 - (8 if self.hint else 0)
        font = ("Segoe UI", 26, "bold")
        for dx, dy in ((2, 2), (-1, -1), (2, -1), (-1, 2)):   # dark outline so it reads on any colour
            self.create_text(cx + dx, cy + dy, text="NO SIGNAL", fill="#000000", font=font, tags="ov")
        self.create_text(cx, cy, text="NO SIGNAL", fill="#ffffff", font=font, tags="ov")
        if self.hint:
            self.create_text(cx, cy + 30, text=self.hint, fill="#c9d1d9", font=("Segoe UI", 9),
                             width=w - 40, justify="center", tags="ov")

    # -- drawing -------------------------------------------------------------------

    def _freq_text(self, hz: float, step: float) -> str:
        """Scale label: enough decimals to tell the grid lines apart."""
        mhz = hz / 1e6
        return f"{mhz:.4f}" if step < 1000 else f"{mhz:.3f}"

    def _static(self):
        """Spectrum background, grid and frequency scale (redrawn when the span changes)."""
        self.delete("static")
        w, h, spec_top, spec_h, wf_top = self._geom()
        bottom = spec_top + spec_h
        self.create_rectangle(0, 0, w, spec_top, fill="#0b0f14", outline="", tags="static")
        self.create_rectangle(0, spec_top, w, bottom + 2, fill=self.SPEC_BG, outline="", tags="static")
        for k in range(1, 4):                                   # level lines
            y = spec_top + spec_h * k / 4
            self.create_line(0, y, w, y, fill=self.GRID, dash=(1, 3), tags="static")
        if self.view:
            lo, hi = self.view
            step = (hi - lo) / 10
            for k in range(11):                                 # 10 divisions, like the radio
                x = w * k / 10
                if 0 < k < 10:
                    self.create_line(x, spec_top, x, bottom, fill=self.GRID, dash=(1, 3), tags="static")
                self.create_line(x, self.AXIS_H - 5, x, self.AXIS_H, fill="#9aa", tags="static")
                if k % 2 == 0 or w > 1100:
                    anchor = "w" if k == 0 else "e" if k == 10 else "center"
                    self.create_text(x + (3 if k == 0 else -3 if k == 10 else 0), 8,
                                     text=self._freq_text(lo + step * k, step), fill="#cfd8e0",
                                     anchor=anchor, font=("Consolas", 9), tags="static")
        self.create_polygon(0, bottom, w, bottom, fill="#1864ab", outline="", tags=("static", "spec"))
        self.create_line(0, bottom, w, bottom, fill="#d0ebff", width=1, tags=("static", "specline"))
        self.tag_lower("img")
        self.tag_raise("static")

    def _overlay(self):
        from .radio import rf_frequency
        self.delete("ov")
        w, h, spec_top, spec_h, wf_top = self._geom()
        bottom = spec_top + spec_h
        if not self.view:
            if self.no_signal:
                self._draw_no_signal(w, h)
            else:
                self.create_text(w / 2, h / 2, text=self.message, fill="#9aa5b1",
                                 font=("Segoe UI", 11), tags="ov")
            return
        st = self.state
        # receive passband (shaded) and frequency marker
        if st and st.ok and st.freq_hz:
            m = (st.mode or "").upper()
            if m.startswith("CW"):
                a, b = st.freq_hz - 300, st.freq_hz + 300
            elif m.startswith("AM"):
                a, b = st.freq_hz - 3000, st.freq_hz + 3000
            elif m.startswith(("FM", "WFM")):
                a, b = st.freq_hz - 7500, st.freq_hz + 7500
            else:
                a = rf_frequency(st.freq_hz, st.mode, 100, st.cw_pitch)
                b = rf_frequency(st.freq_hz, st.mode, 2900, st.cw_pitch)
            if a is not None and b is not None:
                # Passband: thin edge lines and a bracket along the top - no fill,
                # so nothing under it is ever hidden (a filled box looked like a
                # dead spot in the spectrum).
                x0, x1 = sorted((self.hz_to_x(a), self.hz_to_x(b)))
                x1 = max(x1, x0 + 2)
                edge = "#8fb8de"
                for x in (x0, x1):
                    self.create_line(x, spec_top, x, bottom, fill=edge, dash=(2, 2), tags="ov")
                self.create_line(x0, spec_top + 1, x1, spec_top + 1, fill=edge, width=3, tags="ov")
            xd = self.hz_to_x(st.freq_hz)
            colour = "#ff4040" if st.tx else "#ffffff"
            self.create_line(xd, spec_top, xd, bottom, fill=colour, width=1, tags="ov")
            self.create_polygon(xd - 5, spec_top, xd + 5, spec_top, xd, spec_top + 7, fill=colour,
                                outline="", tags="ov")
        # identified signals, labelled at their frequency
        lo, hi = self.view
        end = -1e9
        for rf, text, colour in sorted(self.markers):
            if not lo <= rf <= hi:
                continue
            x = self.hz_to_x(rf)
            self.create_line(x, spec_top - 4, x, spec_top + 4, fill=colour, width=2, tags="ov")
            if x - 30 > end:                                     # don't pile labels on top of each other
                self.create_text(x, self.AXIS_H + self.LABEL_H / 2, text=text, fill=colour,
                                 font=("Segoe UI", 9, "bold"), tags="ov")
                end = x + len(text) * 3.5
        if self.hover is not None:
            hz = self.x_to_hz(self.hover)
            self.create_line(self.hover, spec_top, self.hover, h, fill="#ffff66", dash=(2, 3), tags="ov")
            step = (hi - lo) / 10
            tip = self._freq_text(hz, step / 10) + " MHz" + (" - click to tune" if self.on_click else "")
            anchor = "w" if self.hover < w - 190 else "e"
            self.create_text(self.hover + (6 if anchor == "w" else -6), spec_top + 10, text=tip,
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
