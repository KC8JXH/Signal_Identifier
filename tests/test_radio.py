"""Radio (CAT) reading, tested against pretend radios."""

import socketserver
import threading
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from signal_identifier import radio as R


class FakeSerial:
    """Pretend COM port: answers commands the way a radio would."""

    def __init__(self, responder):
        self.responder = responder
        self.inbox = b""
        self.dtr = self.rts = True

    def reset_input_buffer(self):
        self.inbox = b""

    def write(self, data):
        self.inbox += self.responder(bytes(data))

    @property
    def in_waiting(self):
        return len(self.inbox)

    def read(self, n=1):
        out, self.inbox = self.inbox[:n], self.inbox[n:]
        return out

    def close(self):
        pass


def ic7300(freq=14074000, mode=0x01, data=1, echo=True):
    def bcd_le(v):
        out = []
        for _ in range(5):
            v, r = divmod(v, 100)
            out.append(((r // 10) << 4) | (r % 10))
        return bytes(out)

    def respond(cmd):
        assert cmd[:2] == b"\xfe\xfe" and cmd[2] == 0x94 and cmd[3] == 0xE0
        reply = cmd if echo else b""  # USB echo of our own command
        c = cmd[4]
        if c == 0x03:
            reply += b"\xfe\xfe\xe0\x94\x03" + bcd_le(freq) + b"\xfd"
        elif c == 0x04:
            reply += b"\xfe\xfe\xe0\x94\x04" + bytes([mode, 1]) + b"\xfd"
        elif c == 0x1A and cmd[5] == 0x06:
            reply += b"\xfe\xfe\xe0\x94\x1a\x06" + bytes([data, 1 if data else 0]) + b"\xfd"
        return reply
    return respond


def test_icom_ic7300_usb_data():
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: FakeSerial(ic7300()))
    assert proto.read() == (14074000, "USB-D")


@pytest.mark.parametrize("mode,data,expect", [(0x03, 0, "CW"), (0x00, 0, "LSB"), (0x01, 0, "USB")])
def test_icom_modes(mode, data, expect):
    fs = FakeSerial(ic7300(freq=7030500, mode=mode, data=data, echo=False))
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.read() == (7030500, expect)


def test_icom_does_not_key_transmitter():
    fs = FakeSerial(ic7300())
    R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert fs.dtr is False and fs.rts is False


def test_kenwood():
    def respond(cmd):
        return {b"FA;": b"FA00014074000;", b"MD;": b"MD2;"}.get(cmd, b"?;")
    proto, _ = R.make_protocol("Kenwood TS-590 / TS-890 / TS-990", "COM5",
                               serial_factory=lambda n, b: FakeSerial(respond))
    assert proto.read() == (14074000, "USB")


def test_yaesu_new():
    def respond(cmd):
        return {b"FA;": b"FA014074000;", b"MD0;": b"MD0C;"}.get(cmd, b"?;")
    proto, _ = R.make_protocol("Yaesu FT-991 / FT-991A", "COM6",
                               serial_factory=lambda n, b: FakeSerial(respond))
    assert proto.read() == (14074000, "DATA-U")


def test_yaesu_old():
    def respond(cmd):
        assert cmd == bytes([0, 0, 0, 0, 3])
        return bytes([0x01, 0x40, 0x74, 0x00, 0x0A])  # 14.07400 MHz, DIG
    proto, _ = R.make_protocol("Yaesu FT-817 / 818 / 857 / 897", "COM7",
                               serial_factory=lambda n, b: FakeSerial(respond))
    assert proto.read() == (14074000, "DIG")


def test_silent_radio_returns_nothing():
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: FakeSerial(lambda c: b""))
    assert proto.read() == (None, "")


def test_flrig():
    srv = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
    srv.register_function(lambda: "14074000", "rig.get_vfo")
    srv.register_function(lambda: "USB-D", "rig.get_mode")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proto, _ = R.make_protocol("flrig (share radio with WSJT-X)", net_port=srv.server_address[1])
        assert proto.read() == (14074000.0, "USB-D")
    finally:
        srv.shutdown()


def test_rigctld():
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().strip()
            self.wfile.write({b"f": b"7074000\n", b"m": b"PKTUSB\n3000\n"}[line])

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proto, _ = R.make_protocol("Hamlib rigctld", net_port=srv.server_address[1])
        assert proto.read() == (7074000.0, "PKTUSB")
    finally:
        srv.shutdown()
        srv.server_close()


def test_rf_frequency_and_format():
    assert R.rf_frequency(14074000, "USB-D", 1223) == 14075223
    assert R.rf_frequency(7074000, "LSB", 1000) == 7073000
    assert R.rf_frequency(14074000, "DATA-U", 500) == 14074500
    assert R.rf_frequency(7030000, "CW", 700) is None
    assert R.format_mhz(14075223) == "14.075.223 MHz"
    assert R.format_mhz(7030500) == "7.030.500 MHz"


def test_poller_reports_state():
    import time
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: FakeSerial(ic7300()))
    p = R.RadioPoller(proto, sp, interval=0.05)
    try:
        for _ in range(40):
            if p.get().ok:
                break
            time.sleep(0.05)
        st = p.get()
        assert st.ok and st.freq_hz == 14074000 and st.mode == "USB-D"
    finally:
        p.stop()
