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

    def freq_text(self) -> str:
        return format_mhz(self.freq_hz) if self.freq_hz else "-"


def format_mhz(hz: float) -> str:
    """14074000 -> '14.074.000 MHz' (the way most radios display it)."""
    khz_int = int(round(hz))
    mhz, rest = divmod(khz_int, 1_000_000)
    khz, h = divmod(rest, 1000)
    return f"{mhz}.{khz:03d}.{h:03d} MHz"


def rf_frequency(dial_hz: float | None, mode: str, audio_hz: float) -> float | None:
    """On-air frequency of a signal heard at `audio_hz` in the receiver audio.

    Only worked out for sideband/data modes, where the relationship is simple.
    """
    if not dial_hz or not mode:
        return None
    m = mode.upper().replace("-", "").replace(" ", "")
    lower = ("LSB", "DATAL", "PKTLSB", "LSBD")
    upper = ("USB", "DATAU", "PKTUSB", "USBD", "DIG", "DATA", "PSK")
    if any(m.startswith(x) for x in lower):
        return dial_hz - audio_hz
    if any(m.startswith(x) for x in upper):
        return dial_hz + audio_hz
    return None


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


class IcomCIV(SerialProtocol):
    MODES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY", 0x05: "FM",
             0x06: "WFM", 0x07: "CW-R", 0x08: "RTTY-R", 0x17: "DV", 0x22: "DD"}
    CONTROLLER = 0xE0

    def __init__(self, port, address: int):
        super().__init__(port)
        self.address = address
        self._want = None

    def _frames(self, buf: bytes):
        for raw in buf.split(b"\xfd"):
            i = raw.find(b"\xfe\xfe")
            if i < 0:
                continue
            f = raw[i + 2:].lstrip(b"\xfe")
            if len(f) >= 3:
                yield f  # to, from, cmd, data...

    def _reply(self, buf: bytes, cmd: int):
        for f in self._frames(buf):
            to, frm, c = f[0], f[1], f[2]
            # Skip our own command echoed back by the USB interface.
            if frm == self.address and to in (self.CONTROLLER, 0x00) and c == cmd:
                return f[3:]
        return None

    def _command(self, cmd: int) -> bytes | None:
        msg = bytes([0xFE, 0xFE, self.address, self.CONTROLLER, cmd, 0xFD])
        self._want = cmd
        buf = self._ask(msg, until=b"\xfd", timeout=0.5)
        return self._reply(buf, cmd)

    def _complete(self, buf: bytes) -> bool:
        return self._reply(buf, self._want) is not None

    def read(self):
        data = self._command(0x03)
        freq = _bcd_le_to_int(data[:5]) if data and len(data) >= 4 else None
        mdata = self._command(0x04)
        mode = self.MODES.get(mdata[0], f"mode {mdata[0]:02X}") if mdata else ""
        if mode in ("USB", "LSB"):
            # Data mode (USB-D) on newer Icoms; ignore if the radio doesn't support it.
            d = self._command_sub(0x1A, 0x06)
            if d and d[0] != 0:
                mode += "-D"
        return freq, mode

    def _command_sub(self, cmd: int, sub: int) -> bytes | None:
        msg = bytes([0xFE, 0xFE, self.address, self.CONTROLLER, cmd, sub, 0xFD])
        self._want = cmd
        buf = self._ask(msg, until=b"\xfd", timeout=0.3)
        r = self._reply(buf, cmd)
        if r and len(r) >= 2 and r[0] == sub:
            return r[1:]
        return None


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


class YaesuNew(AsciiCAT):
    MODE_CMD = b"MD0;"
    MODES = {"1": "LSB", "2": "USB", "3": "CW-U", "4": "FM", "5": "AM", "6": "RTTY-L",
             "7": "CW-L", "8": "DATA-L", "9": "RTTY-U", "A": "DATA-FM", "B": "FM-N",
             "C": "DATA-U", "D": "AM-N", "E": "PSK", "F": "DATA-FM-N"}


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
    return Kenwood(sp), sp


class RadioPoller:
    """Asks the radio for frequency and mode about once a second, in the background."""

    def __init__(self, protocol, serial_port=None, interval: float = 1.0):
        self.protocol = protocol
        self.serial_port = serial_port
        self.interval = interval
        self.state = RadioState(message="Connecting…")
        self.running = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        fails = 0
        while self.running:
            try:
                freq, mode = self.protocol.read()
                if freq:
                    fails = 0
                    st = RadioState(freq, mode, True, "")
                else:
                    fails += 1
                    st = RadioState(None, "", False, "Radio isn't answering. Check the port, speed (baud) and radio model.")
            except Exception as e:
                fails += 1
                st = RadioState(None, "", False, f"Radio connection problem: {e}")
            with self.lock:
                if st.ok or fails >= 2 or not self.state.ok:
                    self.state = st
            time.sleep(self.interval)

    def get(self) -> RadioState:
        with self.lock:
            return self.state

    def stop(self):
        self.running = False
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception:
                pass
