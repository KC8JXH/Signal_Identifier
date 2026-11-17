import numpy as np
import pytest

from signal_identifier import synth as s
from signal_identifier.classify import analyze


def noise(seconds=15, level=0.05, seed=0):
    return s.noise(seconds, level, seed)


def modes(audio, fs=s.FS):
    return sorted(d.mode for d in analyze(audio, fs).detections)


def test_noise_only():
    assert modes(noise()) == []
    assert modes(noise(level=0.5)) == []


@pytest.mark.parametrize("wpm,freq", [(12, 550), (20, 700), (30, 600)])
def test_cw(wpm, freq):
    dets = analyze(s.cw(wpm=wpm, freq=freq) + noise(), s.FS).detections
    assert [d.mode for d in dets] == ["CW"]
    assert abs(dets[0].freq_hz - freq) < 15
    assert "WPM" in dets[0].detail


def test_cw_weak():
    assert modes(s.cw(amp=0.02) + noise(seed=3)) == ["CW"]


@pytest.mark.parametrize("shift,mark", [(170, 2125), (850, 1275)])
def test_rtty(shift, mark):
    dets = analyze(s.rtty(mark=mark, shift=shift) + noise(), s.FS).detections
    assert [d.mode for d in dets] == ["RTTY"]
    assert f"{shift} Hz shift" in dets[0].detail


def test_rtty_weak():
    assert modes(s.rtty(amp=0.05) + noise(seed=4)) == ["RTTY"]


def test_ft8_single_and_weak():
    assert modes(s.ft8() + noise()) == ["FT8"]
    assert modes(s.ft8(amp=0.03) + noise(seed=5)) == ["FT8"]


def test_ft8_busy_band():
    bases = [400, 520, 650, 800, 1300, 1500, 1700, 1900]
    x = sum(s.ft8(base=b, seed=i, amp=0.1 + 0.03 * (i % 3)) for i, b in enumerate(bases)) + noise()
    m = modes(x)
    assert m == ["FT8"] * len(bases)


def test_ft4():
    x = np.concatenate([s.ft4(), s.ft4(seed=9)]) + noise()
    assert modes(x) == ["FT4"]


def test_psk31():
    assert modes(s.psk31() + noise()) == ["PSK31"]


def test_carrier():
    assert modes(s.carrier() + noise()) == ["Carrier"]


@pytest.mark.parametrize("seed", [5, 9, 10])
def test_voice(seed):
    assert modes(s.voice(seed=seed) + noise()) == ["SSB voice"]


def test_voice_and_cw_together():
    assert modes(s.voice() + s.cw(freq=3000, amp=0.1) + noise()) == ["CW", "SSB voice"]


def test_48khz_sound_card():
    x = s.ft8() + noise()
    t = np.arange(len(x) * 4) / 48000
    x48 = np.interp(t, np.arange(len(x)) / s.FS, x)
    assert modes(x48, 48000) == ["FT8"]


def test_short_audio():
    assert modes(s.cw(seconds=5) + noise(5)) == ["CW"]
    assert modes(noise(1)) == []
