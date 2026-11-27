"""Window-level checks: press the real buttons (skipped without a display)."""

import os

import pytest

tk = pytest.importorskip("tkinter")

from signal_identifier import radio as R  # noqa: E402


class FakeRadio:
    has_scope = False
    scope_message = ""
    keep_scope_on = True
    tune_status = ""

    def __init__(self, mode="USB"):
        self.mode, self.sent, self.filters, self.spans, self.freqs = mode, [], [], [], []
        self.freq = 14_074_000

    def get(self):
        return R.RadioState(self.freq, self.mode, True, "", -20.0, tx=False, filter=1)

    def tune(self, hz):
        self.freqs.append(hz)
        self.freq = hz

    def set_span(self, half):
        self.spans.append(half)

    def set_mode(self, m, f=None):
        self.sent.append((m, f))
        self.mode = m

    def set_filter(self, n):
        self.filters.append(n)

    def scope(self):
        return None

    def stop(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    from signal_identifier import app as A
    monkeypatch.setattr(A, "SETTINGS_FILE", tmp_path / "settings.json")
    root = tk.Tk()
    a = A.App(root)
    errors = []
    root.report_callback_exception = lambda *e: errors.append(e[1])
    a.radio = FakeRadio()
    a._errors = errors
    yield a
    root.destroy()


def test_every_mode_button_changes_mode(app):
    for name in ("LSB", "CW", "RTTY", "AM", "FM", "USB"):
        app.mode_btns[name].invoke()
    assert [m for m, _ in app.radio.sent] == ["LSB", "CW", "RTTY", "AM", "FM", "USB"]
    assert app._errors == []


def test_data_button_toggles(app):
    app.mode_btns["DATA"].invoke()
    app.mode_btns["DATA"].invoke()
    assert [m for m, _ in app.radio.sent] == ["USB-D", "USB"]


def test_filter_buttons_and_band_memory(app):
    app.filter_btns[3].invoke()
    assert app.radio.filters == [3]
    app.mode_btns["LSB"].invoke()                 # mode change keeps the band's filter
    assert app.radio.sent[-1] == ("LSB", 3)
    assert app._errors == []



def test_band_change_sets_scope_to_500k(app):
    app.radio.has_scope = True
    app.band_btns["40"].invoke()
    assert app.radio.spans == [500_000] and app.span_var.get() == "500000"
    assert app.radio.sent[-1][0] == "LSB"
    app.radio.spans.clear()
    app._band_click(7_150_000)                  # same band: span left alone
    assert app.radio.spans == []


def test_arrows_and_typing_only_widen_when_leaving_the_band(app):
    app.radio.has_scope = True
    app.radio.freq = 14_200_000
    app.step = 5000
    app.tune_pending = None
    app._freq_step(+1)                          # inside 20 m: span left alone
    app._freq_edit()
    app.freq_entry.delete(0, "end")
    app.freq_entry.insert(0, "14.250")
    app._freq_typed(app.freq_entry.get())       # still inside 20 m
    assert app.radio.spans == []
    app._freq_edit()
    app.freq_entry.delete(0, "end")
    app.freq_entry.insert(0, "7.200")
    app._freq_typed(app.freq_entry.get())       # out of 20 m, onto 40 m
    assert app.radio.spans == [500_000]
    assert app._errors == []


def test_tune_log_only_saved_for_no_match(app, tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(app, "_save_tune_log", lambda: saved.append(1) or "")
    app.radio.tune_swr = None
    for status in ("done", "failed"):
        app.radio.tune_status = status
        app._tune_seen = ""
        app._show_tune()
    assert saved == [1]                          # only for "failed"
