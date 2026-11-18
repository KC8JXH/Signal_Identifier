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
from .widgets import BandScope, Waterfall, meter_fraction

WINDOW_SECONDS = 15.0   # how much recent audio each analysis looks at
KEEP_SECONDS = 60.0     # how much audio is kept for "Save recording"
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
        self.ring = RingBuffer(KEEP_SECONDS, self.rate)
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
        self.ring = RingBuffer(KEEP_SECONDS, self.rate)
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
    elif d.mode == "Faint signal":
        text = "Faint"
    else:
        text = d.mode
    return text if d.confidence >= 0.6 else text + "?"


def write_wav(path: Path, audio: np.ndarray, rate: float) -> Path:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(audio, dtype=np.float64)
    peak = np.max(np.abs(x)) if len(x) else 0
    if peak > 1:
        x = x / peak
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(round(rate)))
        w.writeframes(pcm.tobytes())
    return path


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
        self.meter_pos = 0.0
        self.settings = load_settings()
        root.title("Signal Identifier")

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
        self.utc_lbl.pack(fill="x")
        self.lcd_freq = tk.Label(info, text="--.---.--", font=("Consolas", 20, "bold"),
                                 bg="#0b0f14", fg="#4a5560", anchor="e", width=10)
        self.lcd_freq.pack(fill="x")
        self.lcd_mode = tk.Label(info, text="", font=("Consolas", 13, "bold"),
                                 bg="#0b0f14", fg="#39c5ff", anchor="e")
        self.lcd_mode.pack(fill="x")

        controls = tk.Frame(header)
        controls.pack(side="left", fill="x", expand=True)

        # -- row 1: sound input
        top = ttk.Frame(controls, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        ttk.Label(top, text="Sound input:", width=12).pack(side="left")
        self.device_box = ttk.Combobox(top, width=52, state="readonly")
        self.device_box.pack(side="left", padx=6)
        self.start_btn = ttk.Button(top, text="Start listening", command=self.toggle)
        self.start_btn.pack(side="left")
        ttk.Button(top, text="Demo", command=self.start_demo).pack(side="left", padx=6)
        ttk.Button(top, text="Save recording", command=self.save_recording).pack(side="left")

        # -- row 2: radio (CAT) connection
        rrow = ttk.Frame(controls, padding=(8, 2))
        rrow.pack(fill="x")
        ttk.Label(rrow, text="Radio:", width=12).pack(side="left")
        self.radio_box = ttk.Combobox(rrow, values=list(R.RADIOS), width=28, state="readonly")
        self.radio_box.pack(side="left", padx=6)
        self.radio_box.bind("<<ComboboxSelected>>", self._radio_changed)
        ttk.Label(rrow, text="Port:").pack(side="left")
        self.port_box = ttk.Combobox(rrow, width=26, state="readonly")
        self.port_box.pack(side="left", padx=4)
        ttk.Button(rrow, text="↻", width=3, command=self._fill_ports).pack(side="left")
        ttk.Label(rrow, text="Speed:").pack(side="left", padx=(8, 0))
        self.baud_box = ttk.Combobox(rrow, values=[str(b) for b in R.BAUD_RATES], width=8, state="readonly")
        self.baud_box.pack(side="left", padx=4)
        self.radio_btn = ttk.Button(rrow, text="Connect radio", command=self.toggle_radio)
        self.radio_btn.pack(side="left", padx=6)

        # -- row 3: status
        self.status = ttk.Label(controls, text="Pick the sound input your radio is plugged into, then press Start listening.",
                                padding=(8, 2))
        self.status.pack(fill="x")

        # -- row 4: audio level and S-meter bars side by side
        lvl = ttk.Frame(controls, padding=(8, 2, 8, 6))
        lvl.pack(fill="x")
        ttk.Label(lvl, text="Audio level:", width=12).pack(side="left")
        self.level = ttk.Progressbar(lvl, length=200, maximum=100)
        self.level.pack(side="left", padx=6)
        self.level_text = ttk.Label(lvl, text="", width=26)
        self.level_text.pack(side="left")
        ttk.Label(lvl, text="S-meter:").pack(side="left", padx=(12, 0))
        self.smeter = ttk.Progressbar(lvl, length=200, maximum=100)
        self.smeter.pack(side="left", padx=6)
        self.smeter_text = ttk.Label(lvl, text="", width=12)
        self.smeter_text.pack(side="left")

        # -- whole-band view
        bandbar = ttk.Frame(root, padding=(8, 2))
        bandbar.pack(fill="x")
        ttk.Label(bandbar, text="Whole band (from the radio's scope)", font=("Segoe UI", 9, "bold")).pack(side="left")
        self.click_tune = tk.BooleanVar(value=self.settings.get("click_to_tune", True))
        ttk.Checkbutton(bandbar, text="Click to tune the radio", variable=self.click_tune,
                        command=self._save_click_tune).pack(side="left", padx=12)
        ttk.Button(bandbar, text="Show whole band", command=self._whole_band).pack(side="left")
        bf = tk.Frame(root, padx=8)
        bf.pack(fill="x")
        self.band = BandScope(bf, on_click=self._band_click)
        self.band.pack(fill="x")

        # -- detailed audio waterfall fills the rest of the window
        wf = tk.Frame(root, padx=8, pady=8)
        wf.pack(fill="both", expand=True)
        self.wf = Waterfall(wf, fmax=F_HI)
        self.wf.pack(fill="both", expand=True)

        self._fill_devices()
        self._fill_ports()
        self._restore_radio_settings()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(ANALYSE_EVERY_MS, self.tick)
        root.after(100, self.level_tick)
        root.after(200, self.radio_tick)
        root.after(80, self.wf_tick)
        root.after(100, self.band_tick)
        self.clock_tick()
        if demo:
            root.after(200, self.start_demo)

    # -- clock, band view ---------------------------------------------------------

    def clock_tick(self):
        self.utc_lbl.config(text=time.strftime("%H:%M:%S UTC", time.gmtime()))
        self.root.after(250, self.clock_tick)

    def band_tick(self):
        try:
            if self.radio is not None and self.radio.has_scope:
                st = self.radio.get()
                self.band.update_scope(self.radio.scope(), st if st.ok else None)
                if self.band.last_sweep is None and time.monotonic() - self.radio_started > 6:
                    self.band.set_message(self.radio.scope_message or
                                          "No band-scope data from the radio yet. On the IC-7300: MENU > SET > "
                                          "Connectors > CI-V: CI-V USB Port = Unlink from [REMOTE], "
                                          "CI-V USB Baud Rate = 115200.")
            elif self.radio is not None:
                self.band.set_message("The whole-band view needs an Icom radio (IC-7300, IC-705, IC-7610, IC-9700).")
            else:
                self.band.set_message("Connect an Icom radio (like the IC-7300) to see the whole band here.")
        except Exception as e:
            self.band.set_message(f"Band view problem: {e}")
        self.root.after(100, self.band_tick)

    def _save_click_tune(self):
        self.settings["click_to_tune"] = bool(self.click_tune.get())
        save_settings(self.settings)

    def _band_click(self, hz: float):
        from .bandplan import digital_dial_at
        if not self.click_tune.get() or self.radio is None:
            return
        st = self.radio.get()
        if not st.ok:
            return
        mode = st.mode.upper()
        slot = digital_dial_at(hz)
        if mode.startswith("CW"):
            dial = hz                      # CW: tune right onto the signal
        elif slot and not mode.startswith("LSB"):
            dial = slot[1]                 # FT8/FT4 area: the standard dial frequency
        elif "LSB" in mode or mode.endswith("-L"):
            dial = hz + 1500               # put the signal in the middle of the audio
        else:
            dial = hz - 1500
        self.radio.tune(dial)
        self.status.config(text=f"Tuning the radio to {R.format_freq(round(dial / 10) * 10)}")

    def _whole_band(self):
        if self.radio is None or not self.radio.has_scope:
            messagebox.showinfo("Signal Identifier", "Connect an Icom radio first.")
            return
        self.radio.scope_whole_band()
        self.status.config(text="Set the radio's scope to centre mode, ±500 kHz, so the whole band is visible.")

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
        self.radio_started = time.monotonic()
        self.radio_btn.config(text="Disconnect radio")
        self.settings.update(radio=name, radio_port=port, radio_baud=self.baud_box.get(),
                             radio_autoconnect=True)
        save_settings(self.settings)

    def radio_tick(self):
        if self.radio is not None:
            st = self.radio.get()
            if st.ok:
                self.lcd_freq.config(text=st.freq_text(), fg="#f2f2f2")
                self.lcd_mode.config(text=st.mode, fg="#39c5ff")
                if st.cw_pitch and st.mode.upper().startswith("CW"):
                    self.lcd_mode.config(text=f"{st.mode}  pitch {st.cw_pitch:.0f}")
                self._set_meter(st.smeter_db, "" if st.smeter_db is not None else "not available")
                self.wf.set_dial((st.freq_hz, st.mode, st.cw_pitch))
            else:
                self.lcd_freq.config(text="--.---.--", fg="#4a5560")
                self.lcd_mode.config(text="no answer", fg="#ff8787")
                self.status.config(text=st.message)
                self._set_meter(None, "")
                self.wf.set_dial(None)
        else:
            self.lcd_freq.config(text="--.---.--", fg="#4a5560")
            self.lcd_mode.config(text="no radio", fg="#8a96a3")
            self._set_meter(None, "no radio")
            self.wf.set_dial(None)
        self.root.after(100, self.radio_tick)

    def _set_meter(self, db, note: str):
        goal = meter_fraction(db) * 100
        # quick to rise, slower to fall, like a real meter
        self.meter_pos += (goal - self.meter_pos) * (0.6 if goal > self.meter_pos else 0.2)
        self.smeter["value"] = self.meter_pos
        self.smeter_text.config(text=R.s_units_text(db) if db is not None else note)

    def wf_tick(self):
        try:
            if self.source is not None:
                need = self.wf.need_samples(self.source.rate)
                audio = self.source.ring.tail(need)
                if len(audio) >= need:
                    self.wf.add_audio(audio, self.source.rate)
        except Exception as e:
            self.status.config(text=f"Waterfall problem: {e}")
        self.root.after(80, self.wf_tick)

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

    def start_demo(self):
        self.stop()
        self.source = DemoSource()
        self.start_btn.config(text="Stop")
        self.status.config(text="DEMO: playing made-up signals, changing every 20 seconds.")

    def stop(self):
        if self.source is not None:
            self.source.stop()
        self.source = None
        self.last_result = None
        self.start_btn.config(text="Start listening")
        self._show([])
        self.wf.clear()

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
                audio = self.source.ring.tail(int(WINDOW_SECONDS * self.source.rate))
                secs = len(audio) / self.source.rate
                if secs >= 3:
                    self.last_result = analyze(audio, self.source.rate, keep_spectrogram=False)
                    self._show(self.last_result.detections, secs)
                else:
                    self.status.config(text=f"Listening… gathering audio ({secs:.0f} s)")
        except Exception as e:
            self.status.config(text=f"Problem analysing audio: {e}")
        self.root.after(ANALYSE_EVERY_MS, self.tick)

    def _show(self, dets, secs: float = 0):
        """Label each signal on the waterfall with its mode."""
        if self.source is None or not dets:
            self.wf.set_markers([])
            return
        self.wf.set_markers([(d.freq_hz, marker_text(d), MODE_INFO.get(d.mode, ("", "white"))[1]) for d in dets])

    # -- recording -------------------------------------------------------------------

    def save_recording(self):
        """Save the last minute of audio as a WAV file (to study or share)."""
        if self.source is None:
            messagebox.showinfo("Signal Identifier", "Start listening first, then press Save recording.")
            return
        audio = self.source.ring.snapshot()
        if len(audio) < self.source.rate:
            messagebox.showinfo("Signal Identifier", "Not enough audio yet. Wait a few seconds and try again.")
            return
        st = self._radio_state()
        name = time.strftime("%Y-%m-%d_%H%MZ", time.gmtime())
        if st:
            name += f"_{int(round(st.freq_hz))}Hz_{st.mode.replace('/', '-')}"
        try:
            path = write_wav(RECORDINGS_DIR / f"{name}.wav", audio, self.source.rate)
        except Exception as e:
            messagebox.showerror("Signal Identifier", f"Couldn't save the recording:\n\n{e}")
            return
        secs = len(audio) / self.source.rate
        self.status.config(text=f"Saved {secs:.0f} seconds of audio to {path}")
        try:
            import os
            if hasattr(os, "startfile"):
                os.startfile(path.parent)  # open the folder in Windows Explorer
        except Exception:
            pass


def main(demo: bool = False):
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, demo=demo)
    root.mainloop()
