"""The Signal Identifier window."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from .classify import F_HI, analyze

WINDOW_SECONDS = 15.0   # how much recent audio each analysis looks at
ANALYSE_EVERY_MS = 1000
WF_W, WF_H = 720, 260   # waterfall size in pixels

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


def waterfall_ppm(spec_db: np.ndarray, noise_db: np.ndarray, freqs: np.ndarray) -> bytes:
    """Turn a spectrogram into PPM image bytes (newest audio at the top)."""
    rel = spec_db - noise_db[None, :]
    v = (rel + 3) / 30.0
    cols = np.linspace(0, F_HI, WF_W)
    idx = np.clip(np.searchsorted(freqs, cols), 0, len(freqs) - 1)
    v = v[:, idx]
    rows = np.linspace(0, len(v) - 1, WF_H).astype(int)[::-1]
    rgb = _colormap(v[rows])
    return f"P6 {WF_W} {WF_H} 255 ".encode() + rgb.tobytes()


# ---------------------------------------------------------------- window

class App:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.source = None
        root.title("Signal Identifier")
        root.minsize(760, 560)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Sound input:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, width=48, state="readonly")
        self.device_box.pack(side="left", padx=6)
        self.start_btn = ttk.Button(top, text="Start listening", command=self.toggle)
        self.start_btn.pack(side="left")
        ttk.Button(top, text="Demo", command=self.start_demo).pack(side="left", padx=6)

        self.status = ttk.Label(root, text="Pick the sound input your radio is plugged into, then press Start listening.",
                                padding=(8, 0))
        self.status.pack(fill="x")

        lvl = ttk.Frame(root, padding=(8, 4))
        lvl.pack(fill="x")
        ttk.Label(lvl, text="Audio level:").pack(side="left")
        self.level = ttk.Progressbar(lvl, length=220, maximum=100)
        self.level.pack(side="left", padx=6)
        self.level_text = ttk.Label(lvl, text="")
        self.level_text.pack(side="left")

        self.results = tk.Frame(root, padx=8, pady=6)
        self.results.pack(fill="x")
        self.big = tk.Label(self.results, text="Not listening", font=("Segoe UI", 22, "bold"), anchor="w")
        self.big.pack(fill="x")
        self.listbox = tk.Frame(self.results)
        self.listbox.pack(fill="x")

        wf = ttk.Frame(root, padding=8)
        wf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wf, width=WF_W, height=WF_H + 40, bg="#000010", highlightthickness=0)
        self.canvas.pack()
        self.wf_image = None
        self._draw_axis()

        self._fill_devices()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(ANALYSE_EVERY_MS, self.tick)
        root.after(100, self.level_tick)
        if demo:
            root.after(200, self.start_demo)

    # -- devices ---------------------------------------------------------------

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
            pick = next((k for k, (i, _) in enumerate(self.devices) if i == default_in), 0)
            if names:
                self.device_box.current(pick)
            else:
                self.status.config(text="No sound inputs found. Is your radio's USB cable or sound card plugged in?")
        except Exception as e:  # sounddevice missing or no audio system
            self.status.config(text=f"Can't see any sound inputs ({e}). You can still try the Demo.")

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
        self.start_btn.config(text="Start listening")
        self.big.config(text="Not listening", fg="black")
        self._show([])

    def quit(self):
        self.stop()
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
                    result = analyze(audio, self.source.rate)
                    self._draw_waterfall(result)
                    self._show(result.detections, secs)
                else:
                    self.big.config(text=f"Listening… ({secs:.0f} s)")
        except Exception as e:
            self.status.config(text=f"Problem analysing audio: {e}")
        self.root.after(ANALYSE_EVERY_MS, self.tick)

    def _show(self, dets, secs: float = 0):
        for w in self.listbox.winfo_children():
            w.destroy()
        if self.source is None:
            self.canvas.delete("marker")
            return
        self.canvas.delete("marker")
        if not dets:
            self.big.config(text="Nothing heard (just noise)", fg="#555")
            return
        best = max(dets, key=lambda d: d.confidence * (1 + d.strength_db / 30))
        self.big.config(text=best.describe(), fg=MODE_INFO.get(best.mode, ("", "black"))[1])
        ranked = sorted(dets, key=lambda d: -d.confidence)
        MAX_LINES = 6
        for n, d in enumerate(ranked):
            info, colour = MODE_INFO.get(d.mode, ("", "black"))
            if n < MAX_LINES:
                sure = "very likely" if d.confidence >= 0.8 else "likely" if d.confidence >= 0.6 else "maybe"
                line = f"• {d.describe()}: {info} [{sure}]"
                tk.Label(self.listbox, text=line, fg=colour, anchor="w",
                         font=("Segoe UI", 11)).pack(fill="x")
            elif n == MAX_LINES:
                tk.Label(self.listbox, text=f"… and {len(ranked) - MAX_LINES} more (see the labels on the waterfall)",
                         fg="#555", anchor="w", font=("Segoe UI", 11)).pack(fill="x")
            x = d.freq_hz / F_HI * WF_W
            self.canvas.create_text(x, 30, text=d.mode, fill=colour, font=("Segoe UI", 10, "bold"),
                                    tags="marker")
        if secs and secs < 12:
            tk.Label(self.listbox, text="(Still gathering audio; FT8 is clearest after 15 seconds.)",
                     fg="#777", anchor="w").pack(fill="x")

    def _draw_axis(self):
        for f in range(0, int(F_HI) + 1, 500):
            x = f / F_HI * WF_W
            self.canvas.create_line(x, 0, x, 8, fill="#aaa")
            self.canvas.create_text(min(max(x, 14), WF_W - 20), 15, text=f"{f}", fill="#ccc",
                                    font=("Segoe UI", 8))

    def _draw_waterfall(self, result):
        if result.spectrogram_db is None:
            return
        ppm = waterfall_ppm(result.spectrogram_db, result.noise_db, result.freqs_hz)
        img = tk.PhotoImage(data=ppm, format="PPM")
        self.wf_image = img  # keep a reference so Tk doesn't discard it
        self.canvas.delete("wf")
        self.canvas.create_image(0, 40, image=img, anchor="nw", tags="wf")
        self.canvas.tag_raise("marker")


def main(demo: bool = False):
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, demo=demo)
    root.mainloop()
