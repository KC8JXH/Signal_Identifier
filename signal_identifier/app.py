"""The IC7300 Rig Interface window."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from . import radio as R
from .classify import analyze
from .bandplan import BAND_BUTTONS, band_button_for, band_target, mode_after_move
from .recorder import Recorder, elapsed_text
from .widgets import AnalogMeter, BandScope, MeterBar, meter_fraction, swr_fraction

MODE_BUTTONS = ["LSB", "USB", "CW", "RTTY", "AM", "FM", "DATA"]
APP_NAME = "IC7300 Rig Interface"
BTN_BG, BTN_ON = "#e9ecef", "#39c5ff"

WINDOW_SECONDS = 15.0   # how much recent audio each analysis looks at
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"
ANALYSE_EVERY_MS = 1000
SETTINGS_FILE = Path.home() / ".signal_identifier.json"

MODE_INFO = {
    "CW": ("Morse code", "#2b7de9"),
    "SSB voice": ("Voice (single sideband)", "#d9480f"),
    "FT8": ("Digital, 15-second cycles. Decode with WSJT-X.", "#2f9e44"),
    "FT4": ("Digital, 7.5-second cycles (fast FT8). Decode with WSJT-X.", "#37b24d"),
    "RTTY": ("Radioteletype. Decode with fldigi or MMTTY.", "#9c36b5"),
    "PSK31": ("Keyboard-to-keyboard chat. Decode with fldigi.", "#c2255c"),
    "Carrier": ("Steady tone", "#868e96"),
    "SSTV": ("Slow-scan TV picture. Decode with MMSSTV or QSSTV.", "#e64980"),
    "Weather fax": ("Weather fax picture. Decode with fldigi.", "#a61e4d"),
    "Data bursts": ("ARQ data link (Winlink: ARDOP, VARA, PACTOR)", "#e8590c"),
    "Faint signal": ("Something is there, but it's too weak to identify", "#868e96"),
    "Unknown": ("Can't tell what this is yet", "#868e96"),
    "Unknown wide signal": ("Can't tell what this is yet", "#868e96"),
}


# ---------------------------------------------------------------- audio sources

class RingBuffer:
    def __init__(self, seconds: float, rate: float):
        self.rate = rate
        self.buf = np.zeros(int(seconds * rate), dtype=np.float32)
        self.filled = 0
        self.lock = threading.Lock()
        self.last_peak = 0.0
        self.listeners = []   # e.g. a running recording

    def push(self, x: np.ndarray):
        x = np.asarray(x, dtype=np.float32).ravel()
        for listener in list(self.listeners):
            try:
                listener(x)
            except Exception:
                pass
        with self.lock:
            n = len(x)
            if n >= len(self.buf):
                self.buf[:] = x[-len(self.buf):]
            else:
                self.buf = np.roll(self.buf, -n)
                self.buf[-n:] = x
            self.filled = min(len(self.buf), self.filled + n)
            self.last_peak = float(np.max(np.abs(x))) if n else 0.0

    def tail(self, n: int) -> np.ndarray:
        with self.lock:
            n = min(n, self.filled)
            return self.buf[-n:].copy() if n else np.zeros(0, np.float32)

    def snapshot(self) -> np.ndarray:
        with self.lock:
            return self.buf[-self.filled:].copy() if self.filled else np.zeros(0, np.float32)


class SoundCardSource:
    def __init__(self, device: int):
        import sounddevice as sd  # imported here so the demo works without it
        info = sd.query_devices(device)
        self.rate = float(info["default_samplerate"])
        self.ring = RingBuffer(WINDOW_SECONDS, self.rate)
        self.stream = sd.InputStream(device=device, channels=1, samplerate=self.rate,
                                     callback=self._cb, blocksize=0)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        self.ring.push(indata[:, 0])

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class DemoSource:
    """Plays made-up signals of each mode in turn, so you can see the program work."""

    SCENES = ["cw", "ft8_band", "voice", "rtty", "psk31", "ft4", "noise"]
    SCENE_SECONDS = 20

    def __init__(self):
        from . import synth
        self.synth = synth
        self.rate = float(synth.FS)
        self.ring = RingBuffer(WINDOW_SECONDS, self.rate)
        self.running = True
        self.scene_name = ""
        threading.Thread(target=self._run, daemon=True).start()

    def _make(self, name: str, seconds: float) -> np.ndarray:
        s = self.synth
        n = s.noise(seconds, 0.05, seed=int(time.time()))
        if name == "cw":
            return s.cw(seconds, freq=700, wpm=18) + n
        if name == "ft8_band":
            x = n
            for i, base in enumerate([450, 700, 1150, 1400, 1800, 2200]):
                for k in range(int(seconds // 15) + 1):
                    seg = s.ft8(15, base=base, amp=0.08 + 0.04 * (i % 3), seed=i + 10 * k)
                    a = k * len(seg)
                    m = min(len(seg), len(x) - a)
                    if m > 0:
                        x[a:a + m] += seg[:m]
            return x
        if name == "voice":
            return s.voice(seconds) + n
        if name == "rtty":
            return s.rtty(seconds, mark=1500) + n
        if name == "psk31":
            return s.psk31(seconds, freq=1200) + n
        if name == "ft4":
            x = n
            for k in range(int(seconds // 7.5) + 1):
                seg = s.ft4(7.5, base=1000, seed=k)
                a = k * len(seg)
                m = min(len(seg), len(x) - a)
                if m > 0:
                    x[a:a + m] += seg[:m]
            return x
        return n

    def _run(self):
        i = 0
        while self.running:
            self.scene_name = self.SCENES[i % len(self.SCENES)]
            audio = self._make(self.scene_name, self.SCENE_SECONDS)
            chunk = int(self.rate * 0.1)
            for a in range(0, len(audio), chunk):
                if not self.running:
                    return
                self.ring.push(audio[a:a + chunk])
                time.sleep(0.1)
            i += 1

    def stop(self):
        self.running = False


# ---------------------------------------------------------------- colours

def marker_text(d) -> str:
    """Short label for the waterfall."""
    if d.mode.startswith("Unknown"):
        text = f"Data {d.bandwidth_hz:.0f} Hz"
    elif d.mode == "Data bursts":
        text = "ARDOP" if "ARDOP (Winlink)" in d.detail else f"ARQ data {d.bandwidth_hz:.0f} Hz"
    elif d.mode == "Faint signal":
        text = "Faint"
    elif d.mode in ("SSTV", "Weather fax"):
        # e.g. "Martin M1, tones 1.0 kHz too low: tune down 1.0 kHz"
        name, _, rest = d.detail.partition(",")
        advice = rest.split(":")[-1].strip() if ":" in rest else ""
        text = ("SSTV " if d.mode == "SSTV" else "") + name + (f" - {advice}" if advice else "")
    else:
        text = d.mode
    return text if d.confidence >= 0.6 else text + "?"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------- Settings menu helpers

class MenuChoice:
    """A pick-list inside the Settings menu (a submenu of radio items) that
    behaves like the read-only drop-down box it replaced: get(), set(),
    current(), ["values"] and config(state=...). The submenu's own label shows
    the current choice, e.g. "Sound input: USB Audio CODEC"."""

    def __init__(self, parent: tk.Menu, title: str, values=(), on_select=None, extras=()):
        self.parent, self.title, self.on_select, self.extras = parent, title, on_select, list(extras)
        self.menu = tk.Menu(parent, tearoff=0)
        parent.add_cascade(label=title, menu=self.menu)
        self.index = parent.index("end")
        self.var = tk.StringVar()
        self.var.trace_add("write", lambda *a: self._label())
        self._values = list(values)
        self._rebuild()

    def __getitem__(self, key):
        return tuple(self._values) if key == "values" else None

    def __setitem__(self, key, value):
        if key == "values":
            self._values = list(value)
            self._rebuild()

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)

    def current(self, index=None):
        if index is None:
            try:
                return self._values.index(self.var.get())
            except ValueError:
                return -1
        self.var.set(self._values[index])

    def config(self, state=None, **kw):
        if state is not None:
            self.parent.entryconfig(self.index, state="disabled" if state == "disabled" else "normal")

    configure = config

    def bind(self, *a, **kw):
        pass

    def _rebuild(self):
        self.menu.delete(0, "end")
        for v in self._values:
            self.menu.add_radiobutton(label=v, variable=self.var, value=v, command=self._picked)
        if self.extras:
            self.menu.add_separator()
            for label, cmd in self.extras:
                self.menu.add_command(label=label, command=cmd)
        self._label()

    def _label(self):
        v = self.var.get()
        if len(v) > 42:
            v = v[:40] + "…"
        self.parent.entryconfig(self.index, label=f"{self.title}: {v}" if v else self.title)

    def _picked(self):
        if self.on_select:
            self.on_select()


class MenuCommand:
    """A Settings-menu item standing in for a button: config(text=...) renames it."""

    def __init__(self, menu: tk.Menu, label: str, command):
        menu.add_command(label=label, command=command)
        self.menu, self.index = menu, menu.index("end")

    def config(self, text=None, **kw):
        if text is not None:
            self.menu.entryconfig(self.index, label=text)

    configure = config


# ---------------------------------------------------------------- window

class App:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.source = None
        self.radio = None
        self.last_result = None
        self._analysis_thread = None
        self._analysis_result = None
        self.meter_pos = 0.0
        self.settings = load_settings()
        root.title(APP_NAME)

        # Fixed starting size, never resized by the program itself.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1280, sw - 40), min(820, sh - 80)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(900, 600)

        header = tk.Frame(root)
        header.pack(fill="x")

        # -- upper right: UTC time, frequency, mode (stacked)
        info = tk.Frame(header, bg="#0b0f14", padx=14, pady=6)
        info.pack(side="right", anchor="n", padx=8, pady=8)
        self.utc_lbl = tk.Label(info, text="", font=("Consolas", 12), bg="#0b0f14", fg="#8a96a3", anchor="e")
        self.clock_mode = tk.StringVar(value="local" if self.settings.get("clock") == "local" else "utc")
        self.utc_lbl.pack(fill="x")
        frow = tk.Frame(info, bg="#0b0f14")
        frow.pack(fill="x")
        # Click the frequency to type a new one; arrows / mouse wheel step it.
        self.lcd_freq = tk.Label(frow, text="--.---.--", font=("Consolas", 20, "bold"),
                                 bg="#0b0f14", fg="#4a5560", anchor="e", width=10, cursor="xterm")
        self.lcd_freq.pack(side="left", fill="x", expand=True)
        self.lcd_freq.bind("<Button-1>", lambda e: self._freq_edit())
        self.lcd_freq.bind("<MouseWheel>", self._freq_wheel)                   # Windows / Mac
        self.lcd_freq.bind("<Button-4>", lambda e: self._freq_step(+1))        # Linux wheel
        self.lcd_freq.bind("<Button-5>", lambda e: self._freq_step(-1))
        arrows = tk.Frame(frow, bg="#0b0f14")
        arrows.pack(side="left", padx=(6, 0))
        for sym, sign in (("▲", +1), ("▼", -1)):
            tk.Button(arrows, text=sym, font=("Segoe UI", 7), width=2, pady=0, bd=1, bg="#1f2933", fg="#d0d7de",
                      activebackground="#39c5ff", repeatdelay=400, repeatinterval=120,
                      command=lambda s=sign: self._freq_step(s)).pack()
        self.freq_entry = None
        self.tune_pending = None    # (hz, time) of our last tune, shown until the radio confirms
        mrow = tk.Frame(info, bg="#0b0f14")
        mrow.pack(fill="x")
        self.steps = [10, 100, 1000, 5000]
        self.step = self.settings.get("tune_step", 100) if self.settings.get("tune_step") in self.steps else 100
        self.step_lbl = tk.Label(mrow, text="", font=("Consolas", 9), bg="#0b0f14", fg="#8a96a3",
                                 cursor="hand2")
        self.step_lbl.pack(side="left")
        self.step_lbl.bind("<Button-1>", lambda e: self._next_step())
        self._show_step()
        self.lcd_mode = tk.Label(mrow, text="", font=("Consolas", 13, "bold"),
                                 bg="#0b0f14", fg="#39c5ff", anchor="e")
        self.lcd_mode.pack(side="right")

        # -- mode buttons, just left of the display
        modes = tk.Frame(header)
        modes.pack(side="right", anchor="n", pady=8)
        self.mode_btns = {}
        for i, m in enumerate(MODE_BUTTONS):
            b = tk.Button(modes, text=m, width=6, relief="raised", bg=BTN_BG, font=("Segoe UI", 8, "bold"),
                          padx=0, pady=0, command=lambda m=m: self._mode_click(m))
            b.grid(row=i // 2, column=i % 2, padx=1, pady=1)
            self.mode_btns[m] = b
        # Tune: runs the radio's antenna tuner, like holding its TUNER button.
        self.tune_btn = tk.Button(modes, text="TUNE", width=13, relief="raised", bg=BTN_BG, fg="#c92a2a",
                                  font=("Segoe UI", 8, "bold"), padx=0, pady=1, command=self._tune_click)
        self.tune_btn.grid(row=(len(MODE_BUTTONS) + 1) // 2, column=0, columnspan=2, pady=(5, 1))
        self._tune_seen = ""          # last tune result already reported
        self._tune_shown_at = 0.0

        # -- analog meter (S-meter on receive, power on transmit), left of the modes
        self.ameter = AnalogMeter(header)
        self.ameter.pack(side="right", anchor="n", padx=(6, 8), pady=8)
        self.show_ameter = tk.BooleanVar(value=self.settings.get("analog_meter", True))

        # -- receive filter FIL1-3, in the space between the bars and the meter
        fil = tk.Frame(header)
        fil.pack(side="right", anchor="n", pady=8)
        self.fil_frame = fil
        if not self.show_ameter.get():
            self.ameter.pack_forget()
        self.filter_btns = {}
        for n in (1, 2, 3):
            b = tk.Button(fil, text=f"FIL{n}", width=5, relief="raised", bg=BTN_BG,
                          font=("Segoe UI", 8, "bold"), padx=0, pady=0,
                          command=lambda n=n: self._filter_click(n))
            b.pack(pady=1)
            self.filter_btns[n] = b

        controls = tk.Frame(header)
        controls.pack(side="left", fill="x", expand=True)

        # -- row 1: Record and the status line
        top = ttk.Frame(controls, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        self.rec_btn = tk.Button(top, text="● Record", width=16, command=self.toggle_record,
                                 bg=BTN_BG, fg="#c92a2a", font=("Segoe UI", 9, "bold"))
        self.rec_btn.pack(side="left")
        self.recorder = None
        self.status = ttk.Label(top, text="Open Settings (top left) to choose the sound input and start listening.",
                                padding=(10, 0))
        self.status.pack(side="left", fill="x")

        # -- Settings menu: sound input, radio connection, display options, clock
        menubar = tk.Menu(root)
        sm = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=sm)
        root.config(menu=menubar)
        self.settings_menu = sm
        self.start_btn = MenuCommand(sm, "Start listening", self.toggle)
        self.device_box = MenuChoice(sm, "Sound input")
        # Switch the mode labels on the waterfall (and the identifying behind them) on or off.
        self.identify = tk.BooleanVar(value=self.settings.get("identify", True))
        sm.add_checkbutton(label="Identify signals", variable=self.identify, command=self._save_identify)
        sm.add_separator()
        self.radio_btn = MenuCommand(sm, "Connect radio", self.toggle_radio)
        self.radio_box = MenuChoice(sm, "Radio", list(R.RADIOS), on_select=self._radio_changed)
        self.port_box = MenuChoice(sm, "Port", extras=[("Refresh the list", self._fill_ports)])
        self.baud_box = MenuChoice(sm, "Speed", [str(b) for b in R.BAUD_RATES])
        sm.add_separator()
        self.click_tune = tk.BooleanVar(value=self.settings.get("click_to_tune", True))
        sm.add_checkbutton(label="Click to tune radio", variable=self.click_tune, command=self._save_click_tune)
        self.keep_scope = tk.BooleanVar(value=self.settings.get("keep_scope_on", True))
        sm.add_checkbutton(label="Keep scope on", variable=self.keep_scope, command=self._save_keep_scope)
        sm.add_checkbutton(label="Analog meter", variable=self.show_ameter, command=self._save_ameter)
        sm.add_separator()
        sm.add_radiobutton(label="UTC time", variable=self.clock_mode, value="utc", command=self._save_clock)
        sm.add_radiobutton(label="Local time (computer clock)", variable=self.clock_mode, value="local",
                           command=self._save_clock)

        # -- row 4: audio level and S-meter bars side by side
        lvl = ttk.Frame(controls, padding=(8, 2, 8, 6))
        lvl.pack(fill="x")
        ttk.Label(lvl, text="Audio level:", width=12).pack(side="left")
        self.level = ttk.Progressbar(lvl, length=110, maximum=100)
        self.level.pack(side="left", padx=6)
        self.level_text = ttk.Label(lvl, text="", width=14)
        self.level_text.pack(side="left")
        ttk.Label(lvl, text="S-meter:").pack(side="left", padx=(8, 0))
        self.smeter = ttk.Progressbar(lvl, length=110, maximum=100)
        self.smeter.pack(side="left", padx=6)
        self.smeter_text = ttk.Label(lvl, text="", width=9)
        self.smeter_text.pack(side="left")

        # -- row 5: transmit meters (read from the radio while it transmits)
        txr = ttk.Frame(controls, padding=(8, 0, 8, 6))
        txr.pack(fill="x")
        self.tx_meters = {}
        # Po / SWR / ALC / Id (PA drain current), read from the radio while transmitting
        for key, label, zone in (("po", "Po", None), ("swr", "SWR", 0.5), ("alc", "ALC", 2 / 3),
                                 ("id", "Id", 0.8)):
            ttk.Label(txr, text=label + ":").pack(side="left")
            bar = MeterBar(txr, width=56, zone_from=zone)
            bar.pack(side="left", padx=3)
            txt = ttk.Label(txr, text="", width=6)
            txt.pack(side="left", padx=(0, 2))
            self.tx_meters[key] = (bar, txt)

        # -- whole-band view
        bandbar = ttk.Frame(root, padding=(8, 2))
        bandbar.pack(fill="x")
        self.band_btns = {}
        bb = tk.Frame(bandbar)
        bb.pack(side="left", padx=(0, 6))
        for name in BAND_BUTTONS:
            b = tk.Button(bb, text=name, width=4, bg=BTN_BG, font=("Segoe UI", 9, "bold"),
                          command=lambda n=name: self._band_button(n))
            b.pack(side="left", padx=1)
            self.band_btns[name] = b
        # scope span, like the radio's SPAN choices (Center mode)
        spans = tk.Frame(bandbar)
        spans.pack(side="left", padx=(14, 0))
        self.span_var = tk.StringVar(value="")
        self._span_pending = None     # (span, time) just asked for, until the radio shows it
        for half in R.SCOPE_SPANS:
            tk.Checkbutton(spans, text=R.span_label(half), variable=self.span_var, onvalue=str(half),
                           offvalue="", font=("Segoe UI", 8), padx=0, pady=0,
                           command=lambda h=half: self._span_click(h)).pack(side="left", padx=(0, 2))

        # -- the radio's scope (spectrum + waterfall) fills the rest of the window
        bf = tk.Frame(root, padx=8, pady=6)
        bf.pack(fill="both", expand=True)
        self.band = BandScope(bf, on_click=self._band_click)
        self.band.pack(fill="both", expand=True)

        self._fill_devices()
        self._fill_ports()
        self._restore_radio_settings()
        if self.settings.get("listen_autostart") and self.settings.get("sound_input") == self.device_box.get():
            root.after(700, lambda: self.toggle(quiet=True))   # carry on listening like last time
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(ANALYSE_EVERY_MS, self.tick)
        root.after(100, self._collect_analysis)
        root.after(100, self.level_tick)
        root.after(200, self.radio_tick)
        root.after(100, self.band_tick)
        self.clock_tick()
        if demo:
            root.after(200, self.start_demo)

    # -- clock, band view ---------------------------------------------------------

    def clock_tick(self):
        if self.clock_mode.get() == "local":
            self.utc_lbl.config(text=time.strftime("%H:%M:%S", time.localtime()) + " Local")
        else:
            self.utc_lbl.config(text=time.strftime("%H:%M:%S UTC", time.gmtime()))
        if self.recorder is not None:
            self.rec_btn.config(text=f"■ Stop rec  {elapsed_text(self.recorder.seconds)}")
        self.root.after(250, self.clock_tick)

    def band_tick(self):
        try:
            if self.radio is not None and self.radio.has_scope:
                st = self.radio.get()
                self.band.update_scope(self.radio.scope(), st if st.ok else None)
                self._sync_span()
                if self.band.last_sweep is None and time.monotonic() - self.radio_started > 6:
                    self.band.show_no_signal(self.radio.scope_message or
                                             "No band-scope data from the radio yet. On the IC-7300: MENU > SET > "
                                             "Connectors > CI-V: CI-V USB Port = Unlink from [REMOTE], "
                                             "CI-V USB Baud Rate = 115200.")
                elif self.band.no_signal and self.band.last_sweep is not None:
                    self.band.show_no_signal(
                        "Trying to switch the radio's scope back on…" if self.keep_scope.get() else
                        "The radio stopped sending scope data. Turn its scope on, or tick "
                        "Settings > Keep scope on.")
            elif self.radio is not None:
                self.band.set_message("The scope needs an Icom radio (IC-7300, IC-705, IC-7610, IC-9700).")
            else:
                self.band.set_message("Connect the radio (Settings > Connect radio) to see its scope here.")
        except Exception as e:
            self.band.set_message(f"Band view problem: {e}")
        self.root.after(100, self.band_tick)

    def _save_clock(self):
        self.settings["clock"] = self.clock_mode.get()
        save_settings(self.settings)
        self.utc_lbl.config(text="")

    def _save_keep_scope(self):
        self.settings["keep_scope_on"] = bool(self.keep_scope.get())
        save_settings(self.settings)
        if self.radio is not None:
            self.radio.keep_scope_on = self.settings["keep_scope_on"]

    def _save_click_tune(self):
        self.settings["click_to_tune"] = bool(self.click_tune.get())
        save_settings(self.settings)

    def _radio_for_click(self):
        """The radio state if a click may tune it; otherwise say why not."""
        if not self.click_tune.get():
            self.status.config(text="Click-to-tune is switched off (Settings > Click to tune radio).")
            return None
        if self.radio is None:
            self.status.config(text="Connect the radio first to tune by clicking.")
            return None
        st = self.radio.get()
        if not st.ok:
            self.status.config(text="The radio isn't answering, so it can't be tuned.")
            return None
        return st

    # -- typing / stepping the frequency -------------------------------------------

    def _radio_ready(self):
        if self.radio is None:
            self.status.config(text="Connect the radio first to change the frequency.")
            return None
        st = self.radio.get()
        if not st.ok:
            self.status.config(text="The radio isn't answering, so it can't change frequency.")
            return None
        return st

    def _freq_edit(self):
        if self.freq_entry is not None or self._radio_ready() is None:
            return
        e = tk.Entry(self.lcd_freq.master, font=("Consolas", 20, "bold"), bg="#1f2933", fg="#ffffff",
                     insertbackground="#39c5ff", justify="right", relief="flat", width=10)
        e.insert(0, self.lcd_freq.cget("text"))
        e.select_range(0, "end")
        e.place(in_=self.lcd_freq, relx=0, rely=0, relwidth=1, relheight=1)
        e.focus_set()
        e.bind("<Return>", lambda ev: self._freq_typed(e.get()))
        e.bind("<KP_Enter>", lambda ev: self._freq_typed(e.get()))
        e.bind("<Escape>", lambda ev: self._freq_edit_close())
        e.bind("<FocusOut>", lambda ev: self._freq_edit_close())
        self.freq_entry = e
        self.status.config(text="Type a frequency (e.g. 14.074 or 14074) and press Enter. Esc cancels.")

    def _freq_edit_close(self):
        if self.freq_entry is not None:
            self.freq_entry.destroy()
            self.freq_entry = None

    def _freq_typed(self, text: str):
        hz = R.parse_freq(text)
        if hz is None:
            self.status.config(text=f"Couldn't read \"{text}\" as a frequency. Try 14.074 (MHz) or 14074 (kHz).")
            self.freq_entry.select_range(0, "end")
            return
        self._freq_edit_close()
        st = self.radio.get() if self.radio is not None else None
        self._tune_to(hz)
        if st is not None and st.ok:
            self._band_default(st.freq_hz, hz, st.mode)

    def _tune_to(self, hz: float):
        if self._radio_ready() is None:
            return
        hz = round(hz / 10) * 10
        self.radio.tune(hz)
        self.tune_pending = (hz, time.monotonic())
        self.lcd_freq.config(text=R.format_freq(hz))

    def _freq_step(self, sign: int):
        st = self._radio_ready()
        if st is None or not st.freq_hz:
            return
        base = st.freq_hz
        if self.tune_pending and time.monotonic() - self.tune_pending[1] < 1.5:
            base = self.tune_pending[0]       # keep stepping from where we asked to go
        target = R.step_freq(base, self.step, sign)
        self._tune_to(target)
        self._band_default(base, target, st.mode)

    def _band_default(self, old_hz, new_hz, mode):
        """Moved onto another band (or the AM broadcast band): switch to its default mode."""
        new_mode = mode_after_move(old_hz, new_hz, mode)
        if new_mode and self.radio is not None:
            self.radio.set_mode(new_mode)

    def _freq_wheel(self, e):
        self._freq_step(+1 if e.delta > 0 else -1)

    def _show_step(self):
        s = self.step
        self.step_lbl.config(text=f"step {s // 1000} kHz" if s >= 1000 else f"step {s} Hz")

    def _next_step(self):
        self.step = self.steps[(self.steps.index(self.step) + 1) % len(self.steps)]
        self._show_step()
        self.settings["tune_step"] = self.step
        save_settings(self.settings)

    def _tune(self, dial: float | None, st):
        if dial is None:
            self.status.config(text=f"Click-to-tune doesn't work in {st.mode} mode. Switch to USB, LSB, CW or RTTY.")
            return
        self.radio.tune(dial)

    def _band_click(self, hz: float):
        """Click in the scope: the radio goes to the clicked frequency, like
        touching the IC-7300's screen (rounded to suit the span)."""
        st = self._radio_for_click()
        if st is not None:
            # another band (or the AM broadcast band)? then its default mode,
            # and round the click to suit that mode (AM: whole kHz)
            new_mode = mode_after_move(st.freq_hz, hz, st.mode)
            target = R.click_target(hz, new_mode or st.mode, self.band.span_half())
            self._tune(target, st)
            if new_mode and target is not None:
                self.radio.set_mode(new_mode)

    def _span_click(self, half: int):
        """A span tick box: set the radio's scope to Center mode with that span."""
        if self.radio is None or not self.radio.has_scope:
            self.span_var.set("")
            self.status.config(text="Connect the radio first (Settings > Connect radio) to change the scope span.")
            return
        self.span_var.set(str(half))      # clicking the ticked box again keeps it ticked
        self._span_pending = (half, time.monotonic())
        self.radio.set_span(half)

    def _sync_span(self):
        """Tick the box matching the span the radio's scope is showing."""
        if self._span_pending and time.monotonic() - self._span_pending[1] < 3:
            return                         # give the radio a moment to change
        self._span_pending = None
        v = self.band.view
        cur = R.span_of(*v) if v else None
        want = str(cur) if cur else ""
        if self.span_var.get() != want:
            self.span_var.set(want)

    def _fill_devices(self):
        self.devices = []
        try:
            import sounddevice as sd
            apis = sd.query_hostapis()
            default_in = sd.default.device[0]
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    api = apis[d["hostapi"]]["name"]
                    self.devices.append((i, f"{d['name']}  [{api}]"))
            names = [n for _, n in self.devices]
            self.device_box["values"] = names
            saved = self.settings.get("sound_input")
            if saved in names:
                self.device_box.set(saved)
            elif names:
                pick = next((k for k, (i, _) in enumerate(self.devices) if i == default_in), 0)
                # Prefer a radio's built-in USB sound card if there is one.
                usb = next((k for k, n in enumerate(names) if "USB Audio CODEC" in n and "MME" in n), None)
                self.device_box.current(usb if usb is not None else pick)
            else:
                self.status.config(text="No sound inputs found. Is your radio's USB cable or sound card plugged in?")
        except Exception as e:  # sounddevice missing or no audio system
            self.status.config(text=f"Can't see any sound inputs ({e}). You can still try the Demo.")

    # -- radio ---------------------------------------------------------------------

    def _fill_ports(self):
        self.ports = R.list_serial_ports()
        labels = [desc if dev in desc else f"{dev}  {desc}" for dev, desc in self.ports]
        self.port_box["values"] = labels or ["(no COM ports found)"]
        saved = self.settings.get("radio_port")
        k = next((i for i, (dev, _) in enumerate(self.ports) if dev == saved), None)
        if k is None:
            # The IC-7300 shows up as a Silicon Labs CP210x port.
            k = next((i for i, (_, d) in enumerate(self.ports) if "CP210" in d or "Silicon Labs" in d), 0)
        if labels:
            self.port_box.current(k)
        else:
            self.port_box.current(0)

    def _restore_radio_settings(self):
        name = self.settings.get("radio", "Icom IC-7300")
        if name not in R.RADIOS:
            name = "Icom IC-7300"
        self.radio_box.set(name)
        self._radio_changed(keep_baud=True)
        if self.settings.get("radio_autoconnect") and R.RADIOS[name][0] != "none":
            self.root.after(500, self.toggle_radio)

    def _radio_changed(self, event=None, keep_baud=False):
        proto, baud = R.RADIOS[self.radio_box.get()]
        serial_needed = proto not in R.NETWORK_PROTOCOLS and proto != "none"
        state = "readonly" if serial_needed else "disabled"
        self.port_box.config(state=state)
        self.baud_box.config(state=state)
        saved = self.settings.get("radio_baud") if keep_baud else None
        self.baud_box.set(str(saved or baud) if serial_needed else "")

    def toggle_radio(self):
        if self.radio is not None:
            self.radio.stop()
            self.radio = None
            self.radio_btn.config(text="Connect radio")
            self.settings["radio_autoconnect"] = False
            save_settings(self.settings)
            return
        name = self.radio_box.get()
        proto, _ = R.RADIOS[name]
        if proto == "none":
            return
        port = ""
        if proto not in R.NETWORK_PROTOCOLS:
            k = self.port_box.current()
            if not self.ports or k < 0:
                messagebox.showinfo(APP_NAME,
                                    "No COM port found for the radio.\n\nFor an IC-7300: plug in the USB cable, "
                                    "turn the radio on, and install Icom's USB driver if Windows "
                                    "doesn't recognise it. Then press ↻.")
                return
            port = self.ports[k][0]
        try:
            baud = int(self.baud_box.get() or 0)
            protocol, sp = R.make_protocol(name, port, baud)
        except Exception as e:
            hint = ("\n\nIf WSJT-X or another program is using the radio, close it, or choose "
                    "'flrig' as the radio here so both can share it.")
            messagebox.showerror(APP_NAME, f"Couldn't open the radio on {port}:\n\n{e}{hint}")
            return
        self.radio = R.RadioPoller(protocol, sp)
        self.radio.keep_scope_on = bool(self.keep_scope.get())
        self.radio_started = time.monotonic()
        self.radio_btn.config(text="Disconnect radio")
        self.settings.update(radio=name, radio_port=port, radio_baud=self.baud_box.get(),
                             radio_autoconnect=True)
        save_settings(self.settings)

    def radio_tick(self):
        if self.radio is not None:
            st = self.radio.get()
            if st.ok:
                shown = st.freq_text()
                if self.tune_pending and time.monotonic() - self.tune_pending[1] < 1.5:
                    shown = R.format_freq(self.tune_pending[0])   # don't flick back while the radio catches up
                self.lcd_freq.config(text=shown, fg="#f2f2f2")
                self.lcd_mode.config(text=st.mode, fg="#39c5ff")
                if st.cw_pitch and st.mode.upper().startswith("CW"):
                    self.lcd_mode.config(text=f"{st.mode}  pitch {st.cw_pitch:.0f}")
                self._set_meter(st.smeter_db, "" if st.smeter_db is not None else "not available")
                self._set_tx_meters(st)
                self._show_tune()
                self._highlight(st)
            else:
                self.lcd_freq.config(text="--.---.--", fg="#4a5560")
                self.lcd_mode.config(text="no answer", fg="#ff8787")
                self.status.config(text=st.message)
                self._set_meter(None, "")
                self._set_tx_meters(None)
        else:
            self.lcd_freq.config(text="--.---.--", fg="#4a5560")
            self.lcd_mode.config(text="no radio", fg="#8a96a3")
            self._set_meter(None, "no radio")
            self._set_tx_meters(None)
            self._highlight(None)
        self.root.after(100, self.radio_tick)

    def _highlight(self, st):
        cur_mode = st.mode.upper() if st else ""
        for m, b in self.mode_btns.items():
            on = cur_mode == m or (m == "RTTY" and cur_mode in ("RTTY", "RTTY-L", "FSK")) \
                or (m == "USB" and cur_mode in ("USB-D", "DATA-U", "PKTUSB", "DIG")) \
                or (m == "LSB" and cur_mode in ("LSB-D", "DATA-L", "PKTLSB")) \
                or (m == "CW" and cur_mode in ("CW-U", "CW-R", "CW-L")) \
                or (m == "DATA" and (cur_mode.endswith("-D") or cur_mode in ("DATA-U", "DATA-L", "PKTUSB",
                                                                             "PKTLSB", "DIG")))
            b.config(bg=BTN_ON if on else BTN_BG)
        cur_fil = st.filter if st else None
        for n, b in self.filter_btns.items():
            b.config(bg=BTN_ON if n == cur_fil else BTN_BG)
        cur_band = band_button_for(st.freq_hz) if st else None
        for n, b in self.band_btns.items():
            b.config(bg=BTN_ON if n == cur_band else BTN_BG)

    TUNE_LOOK = {   # status -> (button text, background, text colour)
        "starting": ("TUNING…", "#ffa94d", "#000000"),
        "tuning": ("TUNING…", "#ffa94d", "#000000"),
        "done": ("TUNED", "#51cf66", "#000000"),
        "failed": ("NO MATCH", "#ff6b6b", "#ffffff"),
        "refused": ("NO ANSWER", "#ff6b6b", "#ffffff"),
        "unsupported": ("NOT AVAILABLE", "#ced4da", "#495057"),
    }

    def _tune_click(self):
        st = self._radio_ready()
        if st is None:
            return
        if self.radio.tune_status in ("starting", "tuning"):
            return
        if st.tx:
            self.status.config(text="The radio is already transmitting. Wait until it's back on receive, then press Tune.")
            return
        self.radio.start_tune()
        self._tune_seen = ""
        self.status.config(text="Tuning: the radio sends a low-power carrier while its tuner finds a match…")
        self._show_tune()

    def _show_tune(self):
        """Keep the Tune button and status line in step with the radio's tune."""
        status = self.radio.tune_status if self.radio is not None else ""
        if status in ("done", "failed", "refused", "unsupported") and status != self._tune_seen:
            self._tune_seen = status
            self._tune_shown_at = time.monotonic()
            swr = self.radio.tune_swr
            swr_txt = f" (SWR {swr:.1f})" if swr else ""
            self.status.config(text={
                "done": f"Tuned{swr_txt}. Ready to transmit.",
                "failed": f"The tuner couldn't find a match{swr_txt}. Check the antenna and that you're on a ham band.",
                "refused": "The radio didn't start tuning. Check that its internal tuner is available on this band.",
                "unsupported": "Tune works with Icom radios (and through Hamlib rigctld).",
            }[status])
        if status in ("done", "failed", "refused", "unsupported") and time.monotonic() - self._tune_shown_at > 4:
            status = ""   # result shown long enough: back to the normal button
        text, bg, fg = self.TUNE_LOOK.get(status, ("TUNE", BTN_BG, "#c92a2a"))
        self.tune_btn.config(text=text, bg=bg, fg=fg, activebackground=bg)

    def _save_ameter(self):
        """Settings > Analog meter: show or hide the needle meter."""
        on = bool(self.show_ameter.get())
        self.settings["analog_meter"] = on
        save_settings(self.settings)
        if on:
            # back in its place: left of the mode buttons, right of the filter buttons
            self.ameter.pack(side="right", anchor="n", padx=(6, 8), pady=8, before=self.fil_frame)
        else:
            self.ameter.pack_forget()

    def _filter_click(self, n: int):
        if self.radio is None:
            self.status.config(text="Connect the radio first to change the filter.")
            return
        self.radio.set_filter(n)

    def _mode_click(self, mode: str):
        if self.radio is None:
            self.status.config(text="Connect the radio first to change mode.")
            return
        if mode == "DATA":
            # like the radio's DATA key: data mode on/off for the sideband you're on
            st = self.radio.get()
            cur = (st.mode or "").upper()
            if cur.endswith("-D"):
                mode = cur[:-2]
            elif cur in ("USB", "LSB"):
                mode = cur + "-D"
            else:
                self.status.config(text="DATA works in USB or LSB.")
                return
        self.radio.set_mode(mode)
        self.status.config(text=f"Switching the radio to {mode}")

    def _band_button(self, name: str):
        if self.radio is None:
            self.status.config(text="Connect the radio first to change band.")
            return
        st = self.radio.get()
        if not st.ok:
            self.status.config(text="The radio isn't answering, so it can't change band.")
            return
        memory = self.settings.setdefault("band_memory", {})
        here = band_button_for(st.freq_hz)
        if here:
            memory[here] = [st.freq_hz, st.mode]   # remember where we were on this band
        if here == name:
            memory.pop(name, None)                  # pressing the current band again: go to its default spot
        freq, mode = band_target(name, st.mode, memory)
        save_settings(self.settings)
        self.radio.tune(freq)
        self.radio.set_mode(mode)            # every band starts in its default mode
        self.status.config(text=f"Going to {name} m: {R.format_freq(freq)} {mode}")

    def _set_meter(self, db, note: str):
        goal = meter_fraction(db) * 100
        # quick to rise, slower to fall, like a real meter
        self.meter_pos += (goal - self.meter_pos) * (0.6 if goal > self.meter_pos else 0.2)
        self.smeter["value"] = self.meter_pos
        self.smeter_text.config(text=R.s_units_text(db) if db is not None else note)
        st = self.radio.get() if self.radio is not None else None
        if not (st and st.tx):
            self.ameter.set_s(db, R.s_units_text(db) if db is not None else "")

    def _set_tx_meters(self, st):
        """Power / SWR / ALC row. The meters only move while the radio transmits."""
        tx = bool(st and st.tx)
        green, amber, red = "#37b24d", "#f59f00", "#e03131"
        po_bar, po_txt = self.tx_meters["po"]
        swr_bar, swr_txt = self.tx_meters["swr"]
        alc_bar, alc_txt = self.tx_meters["alc"]
        id_bar, id_txt = self.tx_meters["id"]
        if tx:
            # the analog meter becomes a power meter while transmitting
            w = st.power_w
            self.ameter.set_power(w, st.max_power, (f"{w:.1f} W" if w < 10 else f"{w:.0f} W") if w is not None else "")
        if not tx:
            for bar, txt in self.tx_meters.values():
                bar.set(None)
                txt.config(text="")
            if st is not None and st.tx is None and st.ok:
                po_txt.config(text="n/a")
            return
        if st.power_w is not None:
            po_bar.set(st.power_w / (st.max_power or 100), "#1c7ed6")
            po_txt.config(text=f"{st.power_w:.1f} W" if st.power_w < 10 else f"{st.power_w:.0f} W")
        else:
            po_bar.set(None); po_txt.config(text="n/a")
        if st.swr is not None:
            swr_bar.set(swr_fraction(st.swr), red if st.swr >= 2 else amber if st.swr >= 1.5 else green)
            swr_txt.config(text=">10" if st.swr >= 10 else f"{st.swr:.1f}")
        else:
            swr_bar.set(None); swr_txt.config(text="n/a")
        if st.alc is not None:
            # 0-100 % is the ALC zone (first 2/3 of the bar); beyond it you're overdriving
            alc_bar.set(st.alc / 150, red if st.alc > 100 else green)
            alc_txt.config(text="OVER" if st.alc > 100 else f"{st.alc:.0f} %")
        else:
            alc_bar.set(None); alc_txt.config(text="n/a")
        if st.amps is not None:
            # IC-7300 draws about 21 A at 100 W; the shaded part starts at 20 of 25 A
            id_bar.set(st.amps / 25.0, red if st.amps > 22 else "#1c7ed6")
            id_txt.config(text=f"{st.amps:.1f} A")
        else:
            id_bar.set(None); id_txt.config(text="n/a")

    def _radio_state(self):
        if self.radio is None:
            return None
        st = self.radio.get()
        return st if st.ok else None

    # -- start / stop ------------------------------------------------------------

    def toggle(self, quiet: bool = False):
        if self.source is not None:
            self.stop()
            self.settings["listen_autostart"] = False
            save_settings(self.settings)
            return
        k = self.device_box.current()
        if k < 0 or not self.devices:
            if quiet:
                self.status.config(text="Choose the sound input your radio is plugged into (Settings > Sound input), "
                                        "then Settings > Start listening.")
            else:
                messagebox.showinfo(APP_NAME, "Pick a sound input first.")
            return
        try:
            self.source = SoundCardSource(self.devices[k][0])
        except Exception as e:
            self.source = None
            msg = (f"Couldn't open that sound input: {e}. Try another entry in the list "
                   "(the same device often appears several times; the [MME] one usually works).")
            if quiet:
                self.status.config(text=msg)
            else:
                messagebox.showerror(APP_NAME, msg)
            return
        self.settings["sound_input"] = self.devices[k][1]
        self.settings["listen_autostart"] = True
        save_settings(self.settings)
        self.start_btn.config(text="Stop listening")
        self.status.config(text=f"Listening to {self.devices[k][1]}. Results appear after a few seconds.")

    def start_demo(self):
        self.stop()
        self.source = DemoSource()
        self.start_btn.config(text="Stop listening")
        self.status.config(text="DEMO: playing made-up signals, changing every 20 seconds.")  # (python -m signal_identifier --demo)

    def stop(self):
        if self.recorder is not None:
            self.toggle_record()
        if self.source is not None:
            self.source.stop()
        self.source = None
        self.last_result = None
        self.start_btn.config(text="Start listening")
        self._show([])
        self.band.set_markers([])
        self.status.config(text="Not listening. Choose Settings > Start listening.")

    def quit(self):
        self.stop()
        if self.radio is not None:
            self.radio.stop()
        self.root.destroy()

    # -- updates -------------------------------------------------------------------

    def level_tick(self):
        if self.source is not None:
            peak = self.source.ring.last_peak
            db = 20 * np.log10(peak + 1e-9)
            self.level["value"] = max(0, min(100, (db + 60) / 60 * 100))
            if peak >= 0.98:
                msg = "Too loud! Turn the audio down."
            elif db < -50:
                msg = "Very quiet: check the cable and audio settings."
            else:
                msg = "OK"
            self.level_text.config(text=msg)
        else:
            self.level["value"] = 0
            self.level_text.config(text="")
        self.root.after(100, self.level_tick)

    def _save_identify(self):
        self.settings["identify"] = bool(self.identify.get())
        save_settings(self.settings)
        if not self.identify.get():
            self.band.set_markers([])   # take the labels off straight away

    def tick(self):
        try:
            if self.source is not None and not self.identify.get():
                self.band.set_markers([])
            elif self.source is not None:
                audio = self.source.ring.tail(int(WINDOW_SECONDS * self.source.rate))
                secs = len(audio) / self.source.rate
                if secs >= 3:
                    # Identify in the background so the window (and its menus)
                    # stays responsive; _collect_analysis() picks up the result.
                    if self._analysis_thread is None or not self._analysis_thread.is_alive():
                        source = self.source
                        self._analysis_thread = threading.Thread(
                            target=self._analyse_in_background, args=(audio, source.rate, secs, source),
                            daemon=True)
                        self._analysis_thread.start()
                else:
                    self.status.config(text=f"Listening… gathering audio ({secs:.0f} s)")
        except Exception as e:
            self.status.config(text=f"Problem analysing audio: {e}")
        self.root.after(ANALYSE_EVERY_MS, self.tick)

    def _analyse_in_background(self, audio, rate, secs, source):
        try:
            res = analyze(audio, rate, keep_spectrogram=False)
            self._analysis_result = (res, secs, source, None)
        except Exception as e:
            self._analysis_result = (None, secs, source, e)

    def _collect_analysis(self):
        """Runs on the window's thread: show the latest background result."""
        got, self._analysis_result = self._analysis_result, None
        if got is not None:
            res, secs, source, err = got
            # ignore results for audio from before listening was stopped/restarted
            if source is self.source and self.source is not None and self.identify.get():
                if err is not None:
                    self.status.config(text=f"Problem analysing audio: {err}")
                else:
                    self.last_result = res
                    self._show(res.detections, secs)
                    if self.status.cget("text").startswith("Listening… gathering audio"):
                        self.status.config(text="Listening.")
        self.root.after(100, self._collect_analysis)

    def _show(self, dets, secs: float = 0):
        """Label each identified signal on the scope at its frequency."""
        if self.source is None or not dets:
            self.band.set_markers([])
            return
        st = self.radio.get() if self.radio is not None else None
        if st is None or not st.ok or not st.freq_hz:
            # no radio frequency to place them by: list them in the status line
            self.band.set_markers([])
            self.status.config(text="Heard: " + ", ".join(marker_text(d) for d in dets))
            return
        marks = []
        for d in dets:
            rf = R.rf_frequency(st.freq_hz, st.mode, d.freq_hz, st.cw_pitch)
            if rf is None:                     # AM / FM: the sound belongs to the station you're on
                rf = st.freq_hz
            marks.append((rf, marker_text(d), MODE_INFO.get(d.mode, ("", "white"))[1]))
        self.band.set_markers(marks)

    # -- recording -------------------------------------------------------------------

    def toggle_record(self):
        """Start recording the receiver audio to a WAV file, or stop and save it."""
        if self.recorder is not None:
            rec, self.recorder = self.recorder, None
            if self.source is not None and rec.feed in self.source.ring.listeners:
                self.source.ring.listeners.remove(rec.feed)
            path = rec.stop()
            self.rec_btn.config(text="● Record", bg=BTN_BG, fg="#c92a2a")
            self.status.config(text=f"Saved {elapsed_text(rec.seconds)} of audio to {path}")
            try:
                import os
                if hasattr(os, "startfile"):
                    os.startfile(path.parent)  # open the folder in Windows Explorer
            except Exception:
                pass
            return
        if self.source is None:
            messagebox.showinfo(APP_NAME, "Start listening first (Settings > Start listening), then press Record.")
            return
        st = self._radio_state()
        name = time.strftime("%Y-%m-%d_%H%M%SZ", time.gmtime())
        if st:
            name += f"_{int(round(st.freq_hz))}Hz_{st.mode.replace('/', '-')}"
        try:
            self.recorder = Recorder(RECORDINGS_DIR / f"{name}.wav", self.source.rate)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't start recording:\n\n{e}")
            return
        self.source.ring.listeners.append(self.recorder.feed)
        self.rec_btn.config(text="■ Stop rec  0:00", bg="#c92a2a", fg="white")
        self.status.config(text=f"Recording to {self.recorder.path.name}")


def main(demo: bool = False):
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    try:
        root.iconbitmap(default=str(Path(__file__).resolve().parent / "icon.ico"))   # Windows title bar / taskbar
    except tk.TclError:
        pass
    App(root, demo=demo)
    root.mainloop()
