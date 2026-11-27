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
        self.mode, self.sent, self.filters = mode, [], []

    def get(self):
        return R.RadioState(14_074_000, self.mode, True, "", -20.0, tx=False, filter=1)

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
