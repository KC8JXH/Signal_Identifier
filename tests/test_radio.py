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
        self.lock = threading.Lock()
        self.written = []

    def reset_input_buffer(self):
        with self.lock:
            self.inbox = b""

    def write(self, data):
        self.written.append(bytes(data))
        reply = self.responder(bytes(data))
        with self.lock:
            self.inbox += reply

    def push(self, data):  # the radio sending something on its own
        with self.lock:
            self.inbox += data

    @property
    def in_waiting(self):
        return len(self.inbox)

    def read(self, n=1):
        with self.lock:
            out, self.inbox = self.inbox[:n], self.inbox[n:]
        return out

    def close(self):
        pass


def ic7300(freq=14074000, mode=0x01, data=1, echo=True, meter=120, pitch=128,
           tx=None, po=0, swr=0, alc=0, addr=0x94, tuner=None):
    def bcd_le(v):
        out = []
        for _ in range(5):
            v, r = divmod(v, 100)
            out.append(((r // 10) << 4) | (r % 10))
        return bytes(out)

    def respond(cmd):
        assert cmd[:2] == b"\xfe\xfe" and cmd[2] == addr and cmd[3] == 0xE0
        reply = cmd if echo else b""  # USB echo of our own command
        c = cmd[4]
        head = b"\xfe\xfe\xe0" + bytes([addr])

        def bcd2(v):
            return bytes([v // 100, ((v // 10 % 10) << 4) | v % 10])
        if addr != 0x94:
            if c == 0x15 and cmd[5] == 0x11:
                return reply + head + b"\x15\x11" + bcd2(po) + b"\xfd"
            if c == 0x1C and cmd[5] == 0x00 and tx is not None:
                return reply + head + b"\x1c\x00" + bytes([tx]) + b"\xfd"
            return reply
        if c == 0x1C and cmd[5] == 0x01 and tuner is not None:
            if len(cmd) == 8:                       # 1C 01 02: start tuning
                reply += head + b"\xfb\xfd"
            else:                                   # status query: next state in the list
                st = tuner.pop(0) if len(tuner) > 1 else tuner[0]
                reply += head + b"\x1c\x01" + bytes([st]) + b"\xfd"
        elif c == 0x1C and cmd[5] == 0x00 and tx is not None and len(cmd) == 7:
            reply += head + b"\x1c\x00" + bytes([tx]) + b"\xfd"
        elif c == 0x15 and cmd[5] in (0x11, 0x12, 0x13):
            reply += head + bytes([0x15, cmd[5]]) + bcd2({0x11: po, 0x12: swr, 0x13: alc}[cmd[5]]) + b"\xfd"
        elif c == 0x03:
            reply += b"\xfe\xfe\xe0\x94\x03" + bcd_le(freq) + b"\xfd"
        elif c == 0x04:
            reply += b"\xfe\xfe\xe0\x94\x04" + bytes([mode, 1]) + b"\xfd"
        elif c == 0x15 and cmd[5] == 0x02:
            reply += b"\xfe\xfe\xe0\x94\x15\x02" + bytes([meter // 100, ((meter // 10 % 10) << 4) | meter % 10]) + b"\xfd"
        elif c == 0x14 and cmd[5] == 0x09:
            reply += b"\xfe\xfe\xe0\x94\x14\x09" + bytes([pitch // 100, ((pitch // 10 % 10) << 4) | pitch % 10]) + b"\xfd"
        elif c in (0x05, 0x06) or (c == 0x27 and cmd[5] in (0x10, 0x11, 0x14, 0x15)) \
                or (c == 0x1A and cmd[5] == 0x06 and len(cmd) > 7):
            reply += b"\xfe\xfe\xe0\x94\xfb\xfd"
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


def test_yaesu_old_smeter():
    proto, _ = R.make_protocol("Yaesu FT-817 / 818 / 857 / 897", "COM7",
                               serial_factory=lambda n, b: FakeSerial(lambda c: bytes([0x09])))
    assert proto.read_smeter() == 0.0


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
            self.wfile.write({b"f": b"7074000\n", b"m": b"PKTUSB\n3000\n", b"l STRENGTH": b"-12\n"}[line])

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proto, _ = R.make_protocol("Hamlib rigctld", net_port=srv.server_address[1])
        assert proto.read() == (7074000.0, "PKTUSB")
        assert proto.read_smeter() == -12.0
    finally:
        srv.shutdown()
        srv.server_close()


def test_rf_frequency_and_format():
    assert R.rf_frequency(14074000, "USB-D", 1223) == 14075223
    assert R.rf_frequency(7074000, "LSB", 1000) == 7073000
    assert R.rf_frequency(14074000, "DATA-U", 500) == 14074500
    # CW: the radio shows the frequency where a signal sounds at your pitch
    assert R.rf_frequency(7030000, "CW", 700, cw_pitch=600) == 7030100
    assert R.rf_frequency(7030000, "CW", 600, cw_pitch=600) == 7030000
    assert R.rf_frequency(7030000, "CW-R", 700, cw_pitch=600) == 7029900
    assert R.rf_frequency(7200000, "AM", 700) is None
    # same layout as the IC-7300 display
    assert R.format_freq(14074000) == "14.074.00"
    assert R.format_freq(14075223) == "14.075.22"
    assert R.format_freq(7030500) == "7.030.50"
    assert R.format_freq(144174000) == "144.174.00"


def test_icom_smeter_and_pitch():
    for raw, db, text in [(0, -54, "S0"), (120, 0, "S9"), (241, 60, "S9+60"), (60, -27, "S5")]:
        fs = FakeSerial(ic7300(meter=raw))
        proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
        got = proto.read_smeter()
        assert abs(got - db) < 1, (raw, got)
        assert R.s_units_text(got) == text
    fs = FakeSerial(ic7300(pitch=128))
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.read_cw_pitch() == 600


def test_poller_reports_state():
    import time
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: FakeSerial(ic7300()))
    p = R.RadioPoller(proto, sp, interval=0.05)
    try:
        for _ in range(40):
            if p.get().ok:
                break
            time.sleep(0.05)
        for _ in range(40):
            if p.get().smeter_db is not None:
                break
            time.sleep(0.05)
        st = p.get()
        assert st.ok and st.freq_hz == 14074000 and st.mode == "USB-D"
        assert st.smeter_db is not None and abs(st.smeter_db) < 1
    finally:
        p.stop()


def scope_frames(lo, hi, values, centre=False, parts=11):
    """Build the 27 00 frames an IC-7300 sends for one scope sweep."""
    def bcd(n):
        return bytes([((n // 10) << 4) | (n % 10)])
    if centre:
        f1, f2, mode = (lo + hi) // 2, (hi - lo) // 2, 0
    else:
        f1, f2, mode = lo, hi, 1
    out = [b"\xfe\xfe\xe0\x94\x27\x00\x00" + bcd(1) + bcd(parts) + bytes([mode])
           + R._bcd_le(f1) + R._bcd_le(f2) + b"\x00\xfd"]
    chunks = [bytes(values[i:i + 50]) for i in range(0, len(values), 50)]
    for k, ch in enumerate(chunks, start=2):
        out.append(b"\xfe\xfe\xe0\x94\x27\x00\x00" + bcd(k) + bcd(parts) + ch + b"\xfd")
    return b"".join(out)


@pytest.mark.parametrize("centre", [False, True])
def test_icom_band_scope(centre):
    fs = FakeSerial(ic7300())
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.enable_scope(True)
    vals = [i % 160 for i in range(475)]
    fs.push(scope_frames(14_000_000, 14_350_000, vals, centre=centre))
    import time
    for _ in range(50):
        sw = proto.latest_scope()
        if sw:
            break
        time.sleep(0.02)
    assert sw.lo_hz == 14_000_000 and sw.hi_hz == 14_350_000
    assert sw.values == vals
    # replies to questions still work while scope data is flowing
    fs.push(scope_frames(14_000_000, 14_350_000, vals))
    assert proto.read() == (14074000, "USB-D")
    proto.close()


def test_icom_tune_and_span():
    fs = FakeSerial(ic7300())
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.set_freq(14_074_000)
    assert b"\xfe\xfe\x94\xe0\x05" + R._bcd_le(14_074_000) + b"\xfd" in fs.written
    assert proto.scope_whole_span()
    proto.close()


def test_tune_targets():
    # detailed waterfall: exact, even inside the FT8 slot
    assert R.tune_target(14_075_200, "USB-D") == 14_073_700
    # band view: FT8 slot snaps to the standard dial frequency
    assert R.tune_target(14_075_200, "USB-D", snap_digital=True) == 14_074_000
    assert R.tune_target(7_150_000, "LSB") == 7_151_500
    assert R.tune_target(7_030_100, "CW") == 7_030_100
    assert R.tune_target(14_085_000, "RTTY") == 14_085_000
    assert R.tune_target(7_200_000, "AM") is None


def test_waterfall_click_round_trip():
    # click a CW signal heard at 900 Hz with a 600 Hz pitch -> radio moves 300 Hz up
    rf = R.rf_frequency(7_030_000, "CW", 900, 600)
    assert R.tune_target(rf, "CW") == 7_030_300
    # click a signal at 1000 Hz in USB-D at 14.074 -> it ends up at 1500 Hz audio
    rf = R.rf_frequency(14_074_000, "USB-D", 1000)
    new_dial = R.tune_target(rf, "USB-D")
    assert R.rf_frequency(new_dial, "USB-D", 1500) == rf
    # RTTY: the radio shows mark; mark tone heard at 2125 Hz
    assert R.rf_frequency(14_085_000, "RTTY", 2125) == 14_085_000
    assert R.rf_frequency(14_085_000, "RTTY", 2295) == 14_084_830


def test_set_mode_and_freq_commands():
    fs = FakeSerial(ic7300())
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.set_mode("USB-D")
    assert b"\xfe\xfe\x94\xe0\x06\x01\xfd" in fs.written
    assert b"\xfe\xfe\x94\xe0\x1a\x06\x01\x01\xfd" in fs.written
    assert proto.set_mode("CW")
    assert b"\xfe\xfe\x94\xe0\x06\x03\xfd" in fs.written
    proto.close()

    fs = FakeSerial(lambda c: b"")
    k, _ = R.make_protocol("Kenwood TS-590 / TS-890 / TS-990", "COM5", serial_factory=lambda n, b: fs)
    k.set_freq(7_074_000)
    k.set_mode("LSB")
    assert b"FA00007074000;" in fs.written and b"MD1;DA0;" in fs.written

    fs = FakeSerial(lambda c: b"")
    y, _ = R.make_protocol("Yaesu FT-991 / FT-991A", "COM6", serial_factory=lambda n, b: fs)
    y.set_freq(14_074_000)
    y.set_mode("USB-D")
    assert b"FA014074000;" in fs.written and b"MD0C;" in fs.written

    fs = FakeSerial(lambda c: b"")
    o, _ = R.make_protocol("Yaesu FT-817 / 818 / 857 / 897", "COM7", serial_factory=lambda n, b: fs)
    o.set_freq(14_074_000)
    o.set_mode("CW")
    assert bytes([0x01, 0x40, 0x74, 0x00, 0x01]) in fs.written and bytes([0x02, 0, 0, 0, 0x07]) in fs.written


def test_band_buttons():
    from signal_identifier.bandplan import band_button_for, band_target
    assert band_target("20", "USB-D") == (14_074_000, "USB-D")
    assert band_target("40", "USB") == (7_200_000, "LSB")
    assert band_target("20", "LSB") == (14_250_000, "USB")
    assert band_target("30", "LSB") == (10_136_000, "USB-D")   # no phone on 30 m
    assert band_target("40", "CW") == (7_030_000, "CW")
    assert band_target("40", "CW", {"40": (7_012_500, "CW")}) == (7_012_500, "CW")
    assert band_button_for(14_074_000) == "20" and band_button_for(9_000_000) is None


def test_recorder(tmp_path):
    import numpy as np
    from signal_identifier.recorder import Recorder, elapsed_text
    from signal_identifier.__main__ import read_wav
    r = Recorder(tmp_path / "rec" / "a.wav", 48000)
    for _ in range(10):
        r.feed(np.full(4800, 0.25, np.float32))
    path = r.stop()
    x, rate = read_wav(str(path))
    assert rate == 48000 and len(x) == 48000 and abs(x.mean() - 0.25) < 0.01
    assert elapsed_text(83) == "1:23"


# ------------------------------------------------------------- transmit meters

def test_icom_tx_meters():
    fs = FakeSerial(ic7300(tx=1, po=143, swr=48, alc=60))
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.read_tx() is True
    po, swr, alc = proto.read_tx_meters()
    assert abs(po - 50) < 0.5 and abs(swr - 1.5) < 0.01 and abs(alc - 50) < 0.5


def test_icom_meter_calibration():
    assert R.icom_power_w(0, 100) == 0 and abs(R.icom_power_w(213, 100) - 100) < 0.01
    assert abs(R.icom_swr(80) - 2.0) < 0.01 and abs(R.icom_swr(120) - 3.0) < 0.01
    assert R.icom_swr(0) == 1.0 and R.icom_alc(120) == 100


def test_ic705_full_scale_is_10w():
    fs = FakeSerial(ic7300(tx=1, po=213, addr=0xA4))
    proto, _ = R.make_protocol("Icom IC-705", "COM3", serial_factory=lambda n, b: fs)
    assert abs(proto.read_tx_meters()[0] - 10) < 0.05


def test_tx_query_never_keys_the_radio():
    """1C 00 with no data byte only READS the TX state; 1C 00 01 would key the transmitter."""
    fs = FakeSerial(ic7300(tx=0))
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.read_tx() is False
    sent = [w for w in fs.written if len(w) > 4 and w[4] == 0x1C]
    assert sent and all(w == b"\xfe\xfe\x94\xe0\x1c\x00\xfd" for w in sent)


@pytest.mark.parametrize("tx", [0, 1])
def test_poller_tx_meters(tx):
    import time
    fs = FakeSerial(ic7300(tx=tx, po=213, swr=80, alc=150))
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    try:
        for _ in range(80):
            st = p.get()
            if st.ok and st.tx is not None and (not tx or st.power_w is not None):
                break
            time.sleep(0.05)
        st = p.get()
        assert st.ok and st.tx is bool(tx)
        if tx:
            assert abs(st.power_w - 100) < 0.5 and abs(st.swr - 2.0) < 0.01 and st.alc > 100
        else:
            assert st.power_w is None and st.swr is None and st.smeter_db is not None
    finally:
        p.stop()


def test_poller_without_tx_support_keeps_smeter():
    """A radio that doesn't answer the TX query still gets its S-meter read."""
    import time
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: FakeSerial(ic7300()))
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    try:
        for _ in range(60):
            if p.get().smeter_db is not None:
                break
            time.sleep(0.05)
        st = p.get()
        assert st.tx is None and st.smeter_db is not None
    finally:
        p.stop()


def test_rigctld_tx_meters():
    answers = {b"t": b"1\n", b"l RFPOWER_METER_WATTS": b"35.5\n", b"l SWR": b"1.3\n", b"l ALC": b"0.25\n"}

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(answers.get(self.rfile.readline().strip(), b"RPRT -11\n"))

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proto, _ = R.make_protocol("Hamlib rigctld", net_port=srv.server_address[1])
        assert proto.read_tx() is True
        assert proto.read_tx_meters() == (35.5, 1.3, 25.0)
    finally:
        srv.shutdown()
        srv.server_close()


def _scope_on_cmds(fs):
    return sum(1 for w in fs.written if w == b"\xfe\xfe\x94\xe0\x27\x10\x01\xfd")


@pytest.mark.parametrize("keep", [True, False])
def test_poller_turns_scope_back_on(keep):
    """When scope data stops arriving, the poller re-sends 'scope on' (only if allowed)."""
    import time
    fs = FakeSerial(ic7300())
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    p.keep_scope_on = keep
    try:
        for _ in range(40):
            if p.get().ok:
                break
            time.sleep(0.05)
        first = _scope_on_cmds(fs)          # sent once at connect
        assert first == 1
        time.sleep(5.6)                      # no scope data at all -> one retry after 5 s
        assert (_scope_on_cmds(fs) > first) is keep
        assert (p.scope_kicks > 0) is keep
    finally:
        p.stop()


def test_poller_leaves_scope_alone_while_data_flows():
    import threading as th
    import time
    fs = FakeSerial(ic7300())
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    stop = th.Event()

    def feed():
        vals = [50] * 475
        while not stop.is_set():
            fs.push(scope_frames(14_000_000, 14_350_000, vals))
            time.sleep(0.3)
    th.Thread(target=feed, daemon=True).start()
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    try:
        time.sleep(5.8)
        assert p.scope_kicks == 0 and _scope_on_cmds(fs) == 1
    finally:
        stop.set()
        p.stop()



@pytest.mark.parametrize("text,hz", [
    ("14.074", 14_074_000), ("14074", 14_074_000), ("14074000", 14_074_000),
    ("14.074.00", 14_074_000), ("14.074.50", 14_074_500), ("7.0745", 7_074_500),
    ("1840.5", 1_840_500), ("144.174", 144_174_000), ("3573 khz", 3_573_000),
    ("14,230", 14_230_000), (" 21.074 ", 21_074_000), ("50.313MHz", 50_313_000),
])
def test_parse_freq(text, hz):
    assert R.parse_freq(text) == hz


@pytest.mark.parametrize("text", ["", "abc", "14.07x", "0", "999999999999", "14.07.4"])
def test_parse_freq_rejects(text):
    assert R.parse_freq(text) is None



@pytest.mark.parametrize("hz,step,d,want", [
    (14_074_000, 100, +1, 14_074_100), (14_074_000, 100, -1, 14_073_900),
    (14_074_030, 100, +1, 14_074_100), (14_074_030, 100, -1, 14_074_000),
    (14_074_000, 5000, +1, 14_075_000), (14_074_000, 5000, -1, 14_070_000),
    (14_074_010, 10, +1, 14_074_020),
])
def test_step_freq(hz, step, d, want):
    assert R.step_freq(hz, step, d) == want



# ------------------------------------------------------------------ Tune button

def test_icom_start_tune_sends_tuner_command():
    fs = FakeSerial(ic7300(tuner=[2]))
    proto, _ = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    assert proto.start_tune() is True
    assert b"\xfe\xfe\x94\xe0\x1c\x01\x02\xfd" in fs.written
    assert proto.read_tuner() == "tuning"


def test_nothing_transmits_until_tune_is_pressed():
    import time
    fs = FakeSerial(ic7300(tx=0, tuner=[1]))
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    try:
        time.sleep(1.0)
        assert not any(w[4:7] == b"\x1c\x01\x02" for w in fs.written if len(w) > 6)
    finally:
        p.stop()


@pytest.mark.parametrize("states,expect", [([2, 2, 2, 1], "done"), ([2, 2, 0], "failed")])
def test_poller_follows_the_tune(states, expect):
    import time
    fs = FakeSerial(ic7300(tx=0, tuner=list(states)))
    proto, sp = R.make_protocol("Icom IC-7300", "COM3", serial_factory=lambda n, b: fs)
    p = R.RadioPoller(proto, sp, interval=0.05, meter_interval=0.02)
    try:
        for _ in range(40):
            if p.get().ok:
                break
            time.sleep(0.05)
        p.start_tune()
        assert p.tune_status == "starting"
        for _ in range(80):
            if p.tune_status in ("done", "failed"):
                break
            time.sleep(0.05)
        assert p.tune_status == expect
    finally:
        p.stop()


def test_tune_on_a_radio_without_tuner_control():
    import time
    fs = FakeSerial(kenwood_like := (lambda cmd: b""))
    proto = R.Kenwood(fs)
    p = R.RadioPoller(proto, None, interval=0.05)
    try:
        p.start_tune()
        for _ in range(40):
            if p.tune_status not in ("starting",):
                break
            time.sleep(0.05)
        assert p.tune_status == "unsupported"
    finally:
        p.stop()


def test_rigctld_tune():
    answers = {b"G TUNE": b"RPRT 0\n", b"t": b"1\n"}

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(answers.get(self.rfile.readline().strip(), b"RPRT -11\n"))

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proto, _ = R.make_protocol("Hamlib rigctld", net_port=srv.server_address[1])
        assert proto.start_tune() is True
        assert proto.read_tuner() == "tuning"
    finally:
        srv.shutdown()
        srv.server_close()
