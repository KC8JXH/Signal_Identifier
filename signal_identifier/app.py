"""The Signal Identifier window."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from . import radio as R
from .classify import F_HI, analyze

WINDOW_SECONDS = 15.0   # how much recent audio each analysis looks at
ANALYSE_EVERY_MS = 1000
AXIS_H = 40             # space above the waterfall for the scale and labels
RESULT_LINES = 6        # fixed number of result lines, so the window never jumps
SETTINGS_FILE = Path.home() / ".signal_identifier.json"

MODE_INFO = {
    "CW": ("Morse code", "#2b7de9"),
    "SSB voice": ("Voice (single sideband)", "#d9480f"),
    "FT8": ("Digital, 15-second cycles. Decode with WSJT-X.", "#2f9e44"),
    "FT4": ("Digital, 7.5-second cycles (fast FT8). Decode with WSJT-X.", "#37b24d"),
    "RTTY": ("Radioteletype. Decode with fldigi or MMTTY.", "#9c36b5"),
    "PSK31": ("Keyboard-to-keyboard chat. Decode with fldigi.", "#c2255c"),
    "Carrier": ("Steady tone", "#868e96"),
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

    def push(self, x: np.ndarray):
        x = np.asarray(x, dtype=np.float32).ravel()
        with self.lock:
            n = len(x)
            if n >= len(self.buf):
                self.buf[:] = x[-len(self.buf):]
            else:
                self.buf = np.roll(self.buf, -n)
                self.buf[-n:] = x
            self.filled = min(len(self.buf), self.filled + n)
            self.last_peak = float(np.max(np.abs(x))) if n else 0.0

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

def _colormap(v: np.ndarray) -> np.ndarray:
    """0..1 -> RGB (dark blue -> cyan -> yellow -> white), like a rig waterfall."""
    stops = np.array([[0, 0, 30], [0, 40, 140], [0, 180, 220], [250, 230, 40], [255, 255, 255]], float)
    pos = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


def waterfall_ppm(spec_db: np.ndarray, noise_db: np.ndarray, freqs: np.ndarray,
                  width: int, height: int) -> bytes:
    """Turn a spectrogram into PPM image bytes (newest audio at the top)."""
    rel = spec_db - noise_db[None, :]
    v = (rel + 3) / 30.0
    cols = np.linspace(0, F_HI, width)
    idx = np.clip(np.searchsorted(freqs, cols), 0, len(freqs) - 1)
    v = v[:, idx]
    rows = np.linspace(0, len(v) - 1, height).astype(int)[::-1]
    rgb = _colormap(v[rows])
    return f"P6 {width} {height} 255 ".encode() + rgb.tobytes()


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


# ---------------------------------------------------------------- window

class App:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.source = None
        self.radio = None
        self.last_result = None
        self.settings = load_settings()
        root.title("Signal Identifier")

        # Fixed starting size (wide waterfall), never resized by the program itself.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1280, sw - 40), min(820, sh - 80)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(800, 600)

        # -- row 1: sound input
        top = ttk.Frame(root, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        ttk.Label(top, text="Sound input:", width=12).pack(side="left")
        self.device_box = ttk.Combobox(top, width=52, state="readonly")
        self.device_box.pack(side="left", padx=6)
        self.start_btn = ttk.Button(top, text="Start listening", command=self.toggle)
        self.start_btn.pack(side="left")
        ttk.Button(top, text="Demo", command=self.start_demo).pack(side="left", padx=6)

        # -- row 2: radio (CAT) connection
        rrow = ttk.Frame(root, padding=(8, 2))
        rrow.pack(fill="x")
        ttk.Label(rrow, text="Radio:", width=12).pack(side="left")
        self.radio_box = ttk.Combobox(rrow, values=list(R.RADIOS), width=30, state="readonly")
        self.radio_box.pack(side="left", padx=6)
        self.radio_box.bind("<<ComboboxSelected>>", self._radio_changed)
        ttk.Label(rrow, text="Port:").pack(side="left")
        self.port_box = ttk.Combobox(rrow, width=34, state="readonly")
        self.port_box.pack(side="left", padx=4)
        ttk.Button(rrow, text="↻", width=3, command=self._fill_ports).pack(side="left")
        ttk.Label(rrow, text="Speed:").pack(side="left", padx=(8, 0))
        self.baud_box = ttk.Combobox(rrow, values=[str(b) for b in R.BAUD_RATES], width=8, state="readonly")
        self.baud_box.pack(side="left", padx=4)
        self.radio_btn = ttk.Button(rrow, text="Connect radio", command=self.toggle_radio)
        self.radio_btn.pack(side="left", padx=6)

        # -- status + level
        self.status = ttk.Label(root, text="Pick the sound input your radio is plugged into, then press Start listening.",
                                padding=(8, 2))
        self.status.pack(fill="x")
        lvl = ttk.Frame(root, padding=(8, 2))
        lvl.pack(fill="x")
        ttk.Label(lvl, text="Audio level:", width=12).pack(side="left")
        self.level = ttk.Progressbar(lvl, length=220, maximum=100)
        self.level.pack(side="left", padx=6)
        self.level_text = ttk.Label(lvl, text="", width=40)
        self.level_text.pack(side="left")

        # -- radio readout + main answer (fixed heights)
        head = tk.Frame(root, padx=8, pady=4)
        head.pack(fill="x")
        self.radio_lbl = tk.Label(head, text="Radio: not connected", font=("Segoe UI", 14), anchor="w", fg="#555")
        self.radio_lbl.pack(fill="x")
        self.big = tk.Label(head, text="Not listening", font=("Segoe UI", 22, "bold"), anchor="w")
        self.big.pack(fill="x")
        self.lines = []
        for _ in range(RESULT_LINES):
            lab = tk.Label(head, text="", anchor="w", font=("Segoe UI", 11))
            lab.pack(fill="x")
            self.lines.append(lab)

        # -- waterfall fills the rest of the window
        wf = tk.Frame(root, padx=8, pady=8)
        wf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wf, bg="#000010", highlightthickness=0, width=10, height=10)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.wf_image = None

        self._fill_devices()
        self._fill_ports()
        self._restore_radio_settings()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(ANALYSE_EVERY_MS, self.tick)
        root.after(100, self.level_tick)
        root.after(300, self.radio_tick)
        if demo:
            root.after(200, self.start_demo)

    # -- sound devices ---------------------------------------------------------

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
            self.radio_lbl.config(text="Radio: not connected", fg="#555")
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
                messagebox.showinfo("Signal Identifier",
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
            messagebox.showerror("Signal Identifier", f"Couldn't open the radio on {port}:\n\n{e}{hint}")
            return
        self.radio = R.RadioPoller(protocol, sp)
        self.radio_btn.config(text="Disconnect radio")
        self.radio_lbl.config(text="Radio: connecting…", fg="#555")
        self.settings.update(radio=name, radio_port=port, radio_baud=self.baud_box.get(),
                             radio_autoconnect=True)
        save_settings(self.settings)

    def radio_tick(self):
        if self.radio is not None:
            st = self.radio.get()
            if st.ok:
                self.radio_lbl.config(text=f"Radio: {st.freq_text()}   {st.mode}", fg="black")
            else:
                self.radio_lbl.config(text=f"Radio: {st.message}", fg="#c92a2a")
        self.root.after(500, self.radio_tick)

    def _radio_state(self):
        if self.radio is None:
            return None
        st = self.radio.get()
        return st if st.ok else None

    # -- start / stop ------------------------------------------------------------

    def toggle(self):
        if self.source is not None:
            self.stop()
            return
        k = self.device_box.current()
        if k < 0 or not self.devices:
            messagebox.showinfo("Signal Identifier", "Pick a sound input first.")
            return
        try:
            self.source = SoundCardSource(self.devices[k][0])
        except Exception as e:
            self.source = None
            messagebox.showerror("Signal Identifier",
                                 f"Couldn't open that sound input:\n\n{e}\n\n"
                                 "Try another entry in the list (the same device often appears "
                                 "several times; the [MME] one usually works).")
            return
        self.settings["sound_input"] = self.devices[k][1]
        save_settings(self.settings)
        self.start_btn.config(text="Stop")
        self.status.config(text=f"Listening to {self.devices[k][1]}. Results appear after a few seconds.")
        self.big.config(text="Listening…", fg="black")

    def start_demo(self):
        self.stop()
        self.source = DemoSource()
        self.start_btn.config(text="Stop")
        self.status.config(text="DEMO: playing made-up signals, changing every 20 seconds.")
        self.big.config(text="Listening…", fg="black")

    def stop(self):
        if self.source is not None:
            self.source.stop()
        self.source = None
        self.last_result = None
        self.start_btn.config(text="Start listening")
        self.big.config(text="Not listening", fg="black")
        self._show([])
        self.canvas.delete("wf")

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

    def tick(self):
        try:
            if self.source is not None:
                audio = self.source.ring.snapshot()
                secs = len(audio) / self.source.rate
                if secs >= 3:
                    self.last_result = analyze(audio, self.source.rate)
                    self._redraw()
                    self._show(self.last_result.detections, secs)
                else:
                    self.big.config(text=f"Listening… ({secs:.0f} s)")
        except Exception as e:
            self.status.config(text=f"Problem analysing audio: {e}")
        self.root.after(ANALYSE_EVERY_MS, self.tick)

    def _describe(self, d, st) -> str:
        rf = R.rf_frequency(st.freq_hz, st.mode, d.freq_hz) if st else None
        if rf:
            s = f"{d.mode} at {R.format_mhz(rf)}"
            if d.detail:
                s += f" ({d.detail})"
            return s
        return d.describe()

    def _show(self, dets, secs: float = 0):
        for lab in self.lines:
            lab.config(text="")
        self.canvas.delete("marker")
        if self.source is None:
            return
        if not dets:
            self.big.config(text="Nothing heard (just noise)", fg="#555")
            return
        st = self._radio_state()
        best = max(dets, key=lambda d: d.confidence * (1 + d.strength_db / 30))
        self.big.config(text=self._describe(best, st), fg=MODE_INFO.get(best.mode, ("", "black"))[1])
        ranked = sorted(dets, key=lambda d: -d.confidence)
        n_show = RESULT_LINES - 1 if len(ranked) > RESULT_LINES else RESULT_LINES
        for lab, d in zip(self.lines, ranked[:n_show]):
            info, colour = MODE_INFO.get(d.mode, ("", "black"))
            sure = "very likely" if d.confidence >= 0.8 else "likely" if d.confidence >= 0.6 else "maybe"
            audio = f", {d.freq_hz:,.0f} Hz audio" if st and R.rf_frequency(st.freq_hz, st.mode, d.freq_hz) else ""
            lab.config(text=f"• {self._describe(d, st)}: {info} [{sure}{audio}]", fg=colour)
        if len(ranked) > RESULT_LINES:
            self.lines[-1].config(text=f"… and {len(ranked) - n_show} more (see the labels on the waterfall)",
                                  fg="#555")
        elif secs and secs < 12:
            self.lines[-1].config(text="(Still gathering audio; FT8 is clearest after 15 seconds.)", fg="#777")
        self._draw_markers(dets)

    # -- waterfall ---------------------------------------------------------------

    def _size(self):
        return max(100, self.canvas.winfo_width()), max(AXIS_H + 50, self.canvas.winfo_height())

    def _redraw(self):
        self.canvas.delete("axis")
        w, h = self._size()
        for f in range(0, int(F_HI) + 1, 250):
            x = f / F_HI * w
            major = f % 500 == 0
            self.canvas.create_line(x, 0, x, 8 if major else 4, fill="#aaa", tags="axis")
            if major:
                self.canvas.create_text(min(max(x, 14), w - 20), 15, text=f"{f} Hz" if f == 0 else f"{f}",
                                        fill="#ccc", font=("Segoe UI", 8), tags="axis")
        if self.last_result is not None and self.last_result.spectrogram_db is not None and self.source is not None:
            r = self.last_result
            ppm = waterfall_ppm(r.spectrogram_db, r.noise_db, r.freqs_hz, w, h - AXIS_H)
            img = tk.PhotoImage(data=ppm, format="PPM")
            self.wf_image = img  # keep a reference so Tk doesn't discard it
            self.canvas.delete("wf")
            self.canvas.create_image(0, AXIS_H, image=img, anchor="nw", tags="wf")
            self._draw_markers(r.detections)

    def _draw_markers(self, dets):
        self.canvas.delete("marker")
        if self.source is None:
            return
        w, _ = self._size()
        for d in dets:
            colour = MODE_INFO.get(d.mode, ("", "white"))[1]
            x = d.freq_hz / F_HI * w
            self.canvas.create_text(x, 30, text=d.mode, fill=colour, font=("Segoe UI", 10, "bold"),
                                    tags="marker")


def main(demo: bool = False):
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, demo=demo)
    root.mainloop()
