"""Read the dial frequency and mode from the radio (CAT control), read-only.

Supported connections:
  * Icom CI-V (IC-7300, IC-705, IC-7610, IC-9700, IC-7100, ...)
  * Yaesu newer CAT (FT-991A, FT-891, FT-710, FTDX10, FTDX101, ...)
  * Yaesu older CAT (FT-817, FT-818, FT-857, FT-897)
  * Kenwood / Elecraft (TS-590, TS-890, TS-2000, K3, K4, KX2, KX3, ...)
  * flrig (lets WSJT-X, fldigi and this program share the radio)
  * Hamlib rigctld (same idea, used by many logging programs)

Only *reads* are ever sent: this program never changes anything on the radio.
"""

from __future__ import annotations

import socket
import threading
import time
import xmlrpc.client
from dataclasses import dataclass

# ------------------------------------------------------------------ radio list

ICOM_ADDRESSES = {
    "Icom IC-7300": 0x94, "Icom IC-705": 0xA4, "Icom IC-7610": 0x98, "Icom IC-9700": 0xA2,
    "Icom IC-7100": 0x88, "Icom IC-7851": 0x8E, "Icom IC-7600": 0x7A, "Icom IC-7410": 0x80,
    "Icom IC-7200": 0x76, "Icom IC-718": 0x5E, "Icom IC-905": 0xAC,
}

# name -> (protocol, default baud)
RADIOS: dict[str, tuple[str, int]] = {"No radio connection": ("none", 0)}
for _n in ICOM_ADDRESSES:
    RADIOS[_n] = ("icom", 9600 if _n in ("Icom IC-718", "Icom IC-7200") else 115200)
RADIOS.update({
    "Icom (other model)": ("icom", 19200),
    "Yaesu FT-991 / FT-991A": ("yaesu", 38400),
    "Yaesu FT-891": ("yaesu", 38400),
    "Yaesu FT-710": ("yaesu", 38400),
    "Yaesu FTDX10 / FTDX101": ("yaesu", 38400),
    "Yaesu FT-DX3000 / FT-DX1200": ("yaesu", 38400),
    "Yaesu FT-817 / 818 / 857 / 897": ("yaesu_old", 9600),
    "Kenwood TS-590 / TS-890 / TS-990": ("kenwood", 115200),
    "Kenwood TS-2000 / TS-480": ("kenwood", 57600),
    "Elecraft K3 / K4 / KX2 / KX3": ("kenwood", 38400),
    "flrig (share radio with WSJT-X)": ("flrig", 0),
    "Hamlib rigctld": ("rigctld", 0),
})
NETWORK_PROTOCOLS = ("flrig", "rigctld")
BAUD_RATES = [4800, 9600, 19200, 38400, 57600, 115200]


@dataclass
class RadioState:
    freq_hz: float | None = None
    mode: str = ""
    ok: bool = False
    message: str = ""
    smeter_db: float | None = None   # dB relative to S9 (S9 = 0, S1 = -48)
    cw_pitch: float | None = None    # Hz, when the radio tells us

    def freq_text(self) -> str:
        return format_freq(self.freq_hz) if self.freq_hz else "-"


def format_freq(hz: float) -> str:
    """14074000 -> '14.074.00', the way the IC-7300 (and most radios) show it:
    MHz . kHz . tens of Hz."""
    tens = int(round(hz / 10))
    mhz, rest = divmod(tens, 100_000)
    khz, t = divmod(rest, 100)
    return f"{mhz}.{khz:03d}.{t:02d}"


format_mhz = format_freq  # older name


def s_units_text(db: float | None) -> str:
    if db is None:
        return ""
    if db < 5:
        return f"S{min(9, max(0, int(9 + db / 6 + 0.5)))}"
    return f"S9+{int(db / 10 + 0.5) * 10}"


def rf_frequency(dial_hz: float | None, mode: str, audio_hz: float,
                 cw_pitch: float | None = None) -> float | None:
    """On-air frequency of a signal heard at `audio_hz` in the receiver audio,
    matching what the radio would display if you tuned to it.

    SSB/data: dial +/- audio. CW: the radio shows the frequency at which a
    signal sounds at your CW pitch, so dial +/- (audio - pitch).
    """
    if not dial_hz or not mode:
        return None
    m = mode.upper().replace("-", "").replace(" ", "").replace("_", "")
    if m.startswith("CW"):
        pitch = cw_pitch or 600.0
        reverse = m in ("CWR", "CWL", "CWRN")
        return dial_hz - (audio_hz - pitch) if reverse else dial_hz + (audio_hz - pitch)
    if m.startswith(("RTTY", "FSK")):
        # The radio shows the mark frequency; the mark tone is heard at 2125 Hz.
        upper = m in ("RTTYR", "RTTYU", "FSKR")
        return dial_hz + (audio_hz - RTTY_MARK) if upper else dial_hz - (audio_hz - RTTY_MARK)
    lower = ("LSB", "DATAL", "PKTLSB", "LSBD")
    upper = ("USB", "DATAU", "PKTUSB", "USBD", "DIG", "DATA", "PSK", "PKT")
    if any(m.startswith(x) for x in lower):
        return dial_hz - audio_hz
    if any(m.startswith(x) for x in upper):
        return dial_hz + audio_hz
    return None


RTTY_MARK = 2125.0


def tune_target(rf_hz: float, mode: str, snap_digital: bool = False) -> float | None:
    """Dial frequency that puts the signal at `rf_hz` where you want it:
    CW and RTTY right on frequency (your CW pitch / the mark tone), sideband
    and data modes in the middle of the audio (1500 Hz). With `snap_digital`,
    a click inside a standard FT8/FT4 slot tunes to that slot's dial frequency.
    Returns None for modes where this doesn't make sense (AM, FM)."""
    m = mode.upper().replace("-", "").replace(" ", "").replace("_", "")
    if m.startswith(("CW", "RTTY", "FSK")):
        return rf_hz
    lower = m.startswith(("LSB", "DATAL", "PKTLSB"))
    upper = m.startswith(("USB", "DATAU", "PKTUSB", "DIG", "DATA", "PSK", "PKT"))
    if not (lower or upper):
        return None
    if snap_digital and upper:
        from .bandplan import digital_dial_at
        slot = digital_dial_at(rf_hz)
        if slot:
            return float(slot[1])
    return rf_hz + 1500 if lower else rf_hz - 1500


def list_serial_ports() -> list[tuple[str, str]]:
    """[(device, description)], e.g. ('COM4', 'Silicon Labs CP210x USB to UART Bridge (COM4)')."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    return [(p.device, p.description or p.device) for p in ports]


# ------------------------------------------------------------------ protocols

def _bcd_le_to_int(data: bytes) -> int:
    """Icom: least significant byte first, two decimal digits per byte."""
    value = 0
    for i, b in enumerate(data):
        value += ((b >> 4) * 10 + (b & 0x0F)) * (100 ** i)
    return value


def _bcd_be_to_int(data: bytes) -> int:
    value = 0
    for b in data:
        value = value * 100 + (b >> 4) * 10 + (b & 0x0F)
    return value


class SerialProtocol:
    """Base for radios on a serial (COM) port. `port` is anything with
    write(), read(n), reset_input_buffer() and in_waiting (pyserial-compatible)."""

    def __init__(self, port):
        self.port = port

    def _ask(self, data: bytes, until: bytes, timeout: float = 0.5, min_len: int = 0) -> bytes:
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass
        self.port.write(data)
        buf = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            n = getattr(self.port, "in_waiting", 0) or 1
            chunk = self.port.read(n)
            if chunk:
                buf += chunk
                if (until and buf.endswith(until) and len(buf) >= min_len) or (not until and len(buf) >= min_len):
                    if self._complete(buf):
                        return buf
            else:
                time.sleep(0.005)
        return buf

    def _complete(self, buf: bytes) -> bool:
        return True

    def read(self) -> tuple[float | None, str]:
        raise NotImplementedError

    def read_smeter(self) -> float | None:
        """Signal strength in dB relative to S9, or None if not available."""
        return None

    def read_cw_pitch(self) -> float | None:
        return None


@dataclass
class ScopeSweep:
    """One sweep of the radio's band scope."""
    lo_hz: float
    hi_hz: float
    values: list          # 0..160 per point (Icom), left to right
    out_of_range: bool = False


def _bcd_le(v: int, nbytes: int = 5) -> bytes:
    out = []
    for _ in range(nbytes):
        v, r = divmod(v, 100)
        out.append(((r // 10) << 4) | (r % 10))
    return bytes(out)


class IcomCIV(SerialProtocol):
    """Icom CI-V. A background thread reads everything the radio sends, so the
    band-scope data stream and replies to our questions can share the cable."""

    MODES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY", 0x05: "FM",
             0x06: "WFM", 0x07: "CW-R", 0x08: "RTTY-R", 0x17: "DV", 0x22: "DD"}
    CONTROLLER = 0xE0
    OK, NG = 0xFB, 0xFA

    def __init__(self, port, address: int):
        super().__init__(port)
        self.address = address
        self._cv = threading.Condition()
        self._replies: list[bytes] = []   # frames from the radio: cmd, data...
        self._write_lock = threading.Lock()
        self._scope_parts: dict[int, bytes] = {}
        self._scope_header = None
        self._scope: ScopeSweep | None = None
        self._scope_time = 0.0
        self.scope_enabled = False
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    # -- low level ---------------------------------------------------------------

    def _reader(self):
        buf = b""
        while self._running:
            try:
                n = getattr(self.port, "in_waiting", 0) or 1
                chunk = self.port.read(n)
            except Exception:
                time.sleep(0.05)
                continue
            if not chunk:
                time.sleep(0.003)
                continue
            buf += chunk
            while b"\xfd" in buf:
                raw, buf = buf.split(b"\xfd", 1)
                i = raw.find(b"\xfe\xfe")
                if i < 0:
                    continue
                f = raw[i + 2:].lstrip(b"\xfe")
                if len(f) < 3:
                    continue
                to, frm, body = f[0], f[1], f[2:]
                # Ignore our own commands echoed back by the USB interface.
                if frm != self.address or to not in (self.CONTROLLER, 0x00):
                    continue
                if body[:2] == b"\x27\x00":
                    self._scope_frame(body[3:])   # 27 00 00(main scope) seq max ...
                    continue
                with self._cv:
                    self._replies.append(body)
                    del self._replies[:-50]
                    self._cv.notify_all()

    def _send(self, body: bytes, match, timeout: float = 0.5):
        """Send a command and wait for the radio's reply that `match` accepts."""
        with self._cv:
            self._replies.clear()
        with self._write_lock:
            self.port.write(bytes([0xFE, 0xFE, self.address, self.CONTROLLER]) + body + b"\xfd")
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                for r in self._replies:
                    if match(r):
                        self._replies.remove(r)
                        return r
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)

    def _query(self, cmd: int, sub: int | None = None, timeout: float = 0.5) -> bytes | None:
        head = bytes([cmd]) if sub is None else bytes([cmd, sub])
        r = self._send(head, lambda b: b[:len(head)] == head, timeout)
        return r[len(head):] if r is not None else None

    def _set(self, body: bytes, timeout: float = 0.5) -> bool:
        r = self._send(body, lambda b: b[:1] in (bytes([self.OK]), bytes([self.NG])), timeout)
        return r is not None and r[0] == self.OK

    # -- reading -----------------------------------------------------------------

    def read(self):
        data = self._query(0x03)
        freq = _bcd_le_to_int(data[:5]) if data and len(data) >= 4 else None
        mdata = self._query(0x04)
        mode = self.MODES.get(mdata[0], f"mode {mdata[0]:02X}") if mdata else ""
        if mode in ("USB", "LSB"):
            # Data mode (USB-D) on newer Icoms; ignored if the radio doesn't support it.
            d = self._query(0x1A, 0x06, timeout=0.3)
            if d and d[0] != 0:
                mode += "-D"
        return freq, mode

    def read_smeter(self):
        d = self._query(0x15, 0x02, timeout=0.2)
        if not d or len(d) < 2:
            return None
        raw = _bcd_be_to_int(d[:2])  # 0000 = S0, 0120 = S9, 0241 = S9+60
        return icom_meter_db(raw)

    def read_cw_pitch(self):
        d = self._query(0x14, 0x09, timeout=0.3)
        if not d or len(d) < 2:
            return None
        raw = _bcd_be_to_int(d[:2])  # 0000-0255 = 300-900 Hz
        return round((300 + raw * 600 / 255) / 5) * 5

    # -- band scope ----------------------------------------------------------------

    def _scope_frame(self, d: bytes):
        """27 00 data: [seq][seq_max] then header (first part) or points."""
        if len(d) < 2:
            return
        seq, seq_max = _bcd_be_to_int(d[:1]), _bcd_be_to_int(d[1:2])
        rest = d[2:]
        if seq == 1:
            if len(rest) < 11:
                return
            mode = rest[0]
            f1 = _bcd_le_to_int(rest[1:6])
            f2 = _bcd_le_to_int(rest[6:11])
            oor = len(rest) > 11 and rest[11] == 1
            if mode == 0:     # centre mode: frequency +/- span
                lo, hi = f1 - f2, f1 + f2
            else:             # fixed mode: lower and upper edge
                lo, hi = f1, f2
            self._scope_header = (lo, hi, oor)
            self._scope_parts = {}
            if seq_max == 1:  # everything in one frame (some radios / LAN)
                self._finish_scope(rest[12:])
            return
        if self._scope_header is None:
            return
        self._scope_parts[seq] = rest
        if seq == seq_max:
            pts = b"".join(self._scope_parts.get(i, b"") for i in range(2, seq_max + 1))
            self._finish_scope(pts)

    def _finish_scope(self, pts: bytes):
        lo, hi, oor = self._scope_header
        if pts and hi > lo:
            with self._cv:
                self._scope = ScopeSweep(float(lo), float(hi), list(pts), oor)
                self._scope_time = time.monotonic()
        self._scope_parts = {}

    def enable_scope(self, on: bool = True) -> bool:
        ok = True
        if on:
            ok = self._set(b"\x27\x10\x01")           # scope on
        ok = self._set(b"\x27\x11" + (b"\x01" if on else b"\x00")) and ok   # send data over USB
        self.scope_enabled = on and ok
        return ok

    def scope_whole_span(self) -> bool:
        """Centre mode, widest span (+/-500 kHz): covers a whole band around the VFO."""
        a = self._set(b"\x27\x14\x00\x00")
        b = self._set(b"\x27\x15\x00" + _bcd_le(500_000))
        return a and b

    def latest_scope(self, max_age: float = 2.0) -> ScopeSweep | None:
        with self._cv:
            if self._scope is not None and time.monotonic() - self._scope_time <= max_age:
                return self._scope
        return None

    # -- tuning (the only thing we ever change) ---------------------------------

    def set_freq(self, hz: float) -> bool:
        return self._set(b"\x05" + _bcd_le(int(round(hz))))

    def close(self):
        if self.scope_enabled:
            try:
                self.enable_scope(False)
            except Exception:
                pass
        self._running = False


def icom_meter_db(raw: int) -> float:
    """Icom S-meter reading (0-255) -> dB relative to S9."""
    if raw <= 120:
        return (raw / 120 * 9 - 9) * 6
    return (raw - 120) / 121 * 60


def _scale_db(raw: float, s9: float, top: float) -> float:
    """Generic meter: 0..s9 covers S0..S9, s9..top covers S9..S9+60."""
    if raw <= s9:
        return (raw / s9 * 9 - 9) * 6
    return (raw - s9) / (top - s9) * 60


class AsciiCAT(SerialProtocol):
    """Kenwood / Elecraft / newer Yaesu: text commands ending in ';'."""
    MODE_CMD = b"MD;"
    MODES: dict[str, str] = {}

    def _cmd(self, cmd: bytes, prefix: bytes) -> str | None:
        buf = self._ask(cmd, until=b";", timeout=0.5)
        for part in buf.split(b";"):
            part = part.strip()
            if part.startswith(prefix) and len(part) > len(prefix):
                return part[len(prefix):].decode("ascii", "replace")
        return None

    def _complete(self, buf: bytes) -> bool:
        return True

    def read(self):
        f = self._cmd(b"FA;", b"FA")
        freq = float(int(f)) if f and f.isdigit() else None
        m = self._cmd(self.MODE_CMD, self.MODE_CMD[:-1] if self.MODE_CMD != b"MD;" else b"MD")
        mode = self.MODES.get(m[-1:].upper(), f"mode {m}") if m else ""
        return freq, mode


class Kenwood(AsciiCAT):
    MODE_CMD = b"MD;"
    MODES = {"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM", "6": "FSK",
             "7": "CW-R", "9": "FSK-R"}

    def read_smeter(self):
        v = self._cmd(b"SM0;", b"SM0")
        if not v or not v.isdigit():
            return None
        return _scale_db(int(v), 15, 30)  # TS-590 style 0-30, S9 ~ 15 (approximate)


class Elecraft(Kenwood):
    def read_smeter(self):
        v = self._cmd(b"SM;", b"SM")
        if not v or not v.isdigit():
            return None
        return _scale_db(int(v), 9, 21)  # K3/KX3: 0-21 (approximate)


class YaesuNew(AsciiCAT):
    MODE_CMD = b"MD0;"
    MODES = {"1": "LSB", "2": "USB", "3": "CW-U", "4": "FM", "5": "AM", "6": "RTTY-L",
             "7": "CW-L", "8": "DATA-L", "9": "RTTY-U", "A": "DATA-FM", "B": "FM-N",
             "C": "DATA-U", "D": "AM-N", "E": "PSK", "F": "DATA-FM-N"}

    def read_smeter(self):
        v = self._cmd(b"SM0;", b"SM0")
        if not v or not v.isdigit():
            return None
        return _scale_db(int(v), 130, 255)  # 0-255, S9 ~ 130 (approximate)


class YaesuOld(SerialProtocol):
    """FT-817/818/857/897: 5-byte binary commands."""
    MODES = {0x00: "LSB", 0x01: "USB", 0x02: "CW", 0x03: "CW-R", 0x04: "AM", 0x06: "WFM",
             0x08: "FM", 0x0A: "DIG", 0x0C: "PKT", 0x82: "CW-N", 0x83: "CW-R-N", 0x88: "FM-N"}

    def read(self):
        buf = self._ask(bytes([0, 0, 0, 0, 0x03]), until=b"", timeout=0.5, min_len=5)
        if len(buf) < 5:
            return None, ""
        buf = buf[-5:]
        freq = _bcd_be_to_int(buf[:4]) * 10
        mode = self.MODES.get(buf[4], f"mode {buf[4]:02X}")
        return float(freq), mode

    def read_smeter(self):
        buf = self._ask(bytes([0, 0, 0, 0, 0xE7]), until=b"", timeout=0.3, min_len=1)
        if not buf:
            return None
        n = buf[-1] & 0x0F  # 0-9 = S0-S9, 10-15 = +10..+60
        return (n - 9) * 6.0 if n <= 9 else (n - 9) * 10.0


class _QuickTransport(xmlrpc.client.Transport):
    """Give up after 2 seconds instead of hanging if flrig isn't running."""

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = 2.0
        return conn


class Flrig:
    def __init__(self, host="127.0.0.1", port=12345):
        self.proxy = xmlrpc.client.ServerProxy(f"http://{host}:{port}", allow_none=True,
                                               transport=_QuickTransport())

    def read(self):
        f = self.proxy.rig.get_vfo()
        m = self.proxy.rig.get_mode()
        return float(f), str(m)

    def read_smeter(self):
        try:
            v = float(self.proxy.rig.get_smeter())
        except Exception:
            return None
        return _scale_db(v, 50, 100)  # flrig: 0-100, S9 at 50


class Rigctld:
    def __init__(self, host="127.0.0.1", port=4532):
        self.addr = (host, port)

    def _query(self, cmd: str) -> list[str]:
        with socket.create_connection(self.addr, timeout=1.0) as s:
            s.sendall(cmd.encode() + b"\n")
            data = b""
            want = 2 if cmd == "m" else 1
            end = time.monotonic() + 1.0
            while data.count(b"\n") < want and time.monotonic() < end:
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
        return data.decode("ascii", "replace").split()

    def read(self):
        f = self._query("f")
        m = self._query("m")
        if not f or f[0].startswith("RPRT"):
            raise IOError("rigctld reported an error: " + " ".join(f))
        return float(f[0]), (m[0] if m and not m[0].startswith("RPRT") else "")

    def read_smeter(self):
        try:
            v = self._query("l STRENGTH")
            return float(v[0]) if v and not v[0].startswith("RPRT") else None
        except Exception:
            return None


# ------------------------------------------------------------------ poller

def make_protocol(radio: str, port_name: str = "", baud: int = 0, address: int | None = None,
                  host: str = "127.0.0.1", net_port: int | None = None, serial_factory=None):
    proto, default_baud = RADIOS[radio]
    if proto == "none":
        return None, None
    if proto == "flrig":
        return Flrig(host, net_port or 12345), None
    if proto == "rigctld":
        return Rigctld(host, net_port or 4532), None
    if serial_factory is None:
        import serial  # pyserial

        def serial_factory(name, rate):
            sp = serial.Serial()
            sp.port, sp.baudrate, sp.timeout = name, rate, 0.05
            sp.stopbits = 2 if proto == "yaesu_old" else 1
            sp.rtscts = sp.dsrdtr = False
            # Set these BEFORE opening: on some setups DTR/RTS key the transmitter.
            sp.dtr = False
            sp.rts = False
            sp.open()
            return sp
    sp = serial_factory(port_name, baud or default_baud)
    try:
        sp.dtr = False  # some interfaces key the transmitter on DTR/RTS
        sp.rts = False
    except Exception:
        pass
    if proto == "icom":
        addr = address if address is not None else ICOM_ADDRESSES.get(radio, 0x94)
        return IcomCIV(sp, addr), sp
    if proto == "yaesu":
        return YaesuNew(sp), sp
    if proto == "yaesu_old":
        return YaesuOld(sp), sp
    if radio.startswith("Elecraft"):
        return Elecraft(sp), sp
    return Kenwood(sp), sp


class RadioPoller:
    """Reads the radio in the background: S-meter several times a second,
    frequency and mode about once a second, CW pitch now and then."""

    def __init__(self, protocol, serial_port=None, interval: float = 1.0, meter_interval: float = 0.2):
        self.protocol = protocol
        self.serial_port = serial_port
        self.interval = interval
        self.meter_interval = meter_interval
        self.state = RadioState(message="Connecting…")
        self.running = True
        self.lock = threading.Lock()
        self._tune_to = None
        self._scope_cmd = None
        self.scope_message = ""
        self.has_scope = hasattr(protocol, "latest_scope")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        if self.has_scope:
            try:
                if not self.protocol.enable_scope(True):
                    self.scope_message = "The radio didn't accept the band-scope request."
            except Exception as e:
                self.scope_message = f"Band scope problem: {e}"
        fails = 0
        last_full = 0.0
        last_pitch = -1e9
        meter_ok = True
        pitch = None
        while self.running:
            now = time.monotonic()
            self._do_requests()
            if now - last_full >= self.interval:
                last_full = now
                try:
                    freq, mode = self.protocol.read()
                    if freq:
                        fails = 0
                        if mode.upper().startswith("CW") and now - last_pitch > 10:
                            last_pitch = now
                            try:
                                pitch = self.protocol.read_cw_pitch() or pitch
                            except Exception:
                                pass
                        with self.lock:
                            old = self.state
                            self.state = RadioState(freq, mode, True, "", old.smeter_db, pitch)
                    else:
                        fails += 1
                        self._fail("Radio isn't answering. Check the port, speed (baud) and radio model.", fails)
                except Exception as e:
                    fails += 1
                    self._fail(f"Radio connection problem: {e}", fails)
            if meter_ok and self.state.ok:
                try:
                    db = self.protocol.read_smeter()
                    with self.lock:
                        self.state.smeter_db = db
                except Exception:
                    meter_ok = False  # radio doesn't support it; stop asking
            time.sleep(self.meter_interval if meter_ok and self.state.ok else min(self.interval, 0.5))

    def _fail(self, msg: str, fails: int):
        with self.lock:
            if fails >= 2 or not self.state.ok:
                self.state = RadioState(None, "", False, msg)

    def _do_requests(self):
        with self.lock:
            hz, self._tune_to = self._tune_to, None
            sc, self._scope_cmd = self._scope_cmd, None
        if hz and hasattr(self.protocol, "set_freq"):
            try:
                if self.protocol.set_freq(hz):
                    with self.lock:
                        self.state.freq_hz = hz
            except Exception:
                pass
        if sc == "whole" and hasattr(self.protocol, "scope_whole_span"):
            try:
                if not self.protocol.scope_whole_span():
                    self.scope_message = "The radio didn't accept the scope span change."
            except Exception as e:
                self.scope_message = f"Band scope problem: {e}"

    def tune(self, hz: float):
        """Ask the radio to move to `hz` (done in the background)."""
        with self.lock:
            self._tune_to = float(round(hz / 10) * 10)

    def scope_whole_band(self):
        with self.lock:
            self._scope_cmd = "whole"

    def scope(self):
        return self.protocol.latest_scope() if self.has_scope else None

    def get(self) -> RadioState:
        with self.lock:
            st = self.state
            return RadioState(st.freq_hz, st.mode, st.ok, st.message, st.smeter_db, st.cw_pitch)

    def stop(self):
        self.running = False
        try:
            self.thread.join(timeout=1.5)
        except Exception:
            pass
        if hasattr(self.protocol, "close"):
            try:
                self.protocol.close()
            except Exception:
                pass
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception:
                pass
