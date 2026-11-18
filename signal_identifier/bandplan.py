"""Amateur band edges and where each mode is usually found (US / ITU Region 2).

These are only hints for the band view; real activity varies.
"""

from __future__ import annotations

# (low Hz, high Hz, name)
BANDS = [
    (1_800_000, 2_000_000, "160 m"), (3_500_000, 4_000_000, "80 m"),
    (5_330_000, 5_410_000, "60 m"), (7_000_000, 7_300_000, "40 m"),
    (10_100_000, 10_150_000, "30 m"), (14_000_000, 14_350_000, "20 m"),
    (18_068_000, 18_168_000, "17 m"), (21_000_000, 21_450_000, "15 m"),
    (24_890_000, 24_990_000, "12 m"), (28_000_000, 29_700_000, "10 m"),
    (50_000_000, 54_000_000, "6 m"),
]

# Digital modes: the usual *dial* frequency (USB). Signals sit 0-3 kHz above it.
DIGITAL_DIALS = {
    "FT8": [1_840_000, 3_573_000, 5_357_000, 7_074_000, 10_136_000, 14_074_000, 18_100_000,
            21_074_000, 24_915_000, 28_074_000, 50_313_000],
    "FT4": [3_575_000, 7_047_500, 10_140_000, 14_080_000, 18_104_000, 21_140_000,
            24_919_000, 28_180_000, 50_318_000],
}

# (low Hz, high Hz, label) - broad segments
SEGMENTS = [
    (1_800_000, 1_840_000, "CW"), (1_843_000, 2_000_000, "SSB"),
    (3_500_000, 3_570_000, "CW"), (3_580_000, 3_600_000, "RTTY/data"), (3_600_000, 4_000_000, "SSB"),
    (7_000_000, 7_045_000, "CW"), (7_080_000, 7_125_000, "RTTY/data"), (7_125_000, 7_300_000, "SSB"),
    (10_100_000, 10_130_000, "CW"), (10_142_000, 10_150_000, "RTTY/data"),
    (14_000_000, 14_070_000, "CW"), (14_083_000, 14_112_000, "RTTY/data"), (14_150_000, 14_350_000, "SSB"),
    (18_068_000, 18_095_000, "CW"), (18_110_000, 18_168_000, "SSB"),
    (21_000_000, 21_070_000, "CW"), (21_080_000, 21_110_000, "RTTY/data"), (21_200_000, 21_450_000, "SSB"),
    (24_890_000, 24_910_000, "CW"), (24_930_000, 24_990_000, "SSB"),
    (28_000_000, 28_070_000, "CW"), (28_080_000, 28_150_000, "RTTY/data"), (28_300_000, 29_700_000, "SSB"),
    (50_000_000, 50_100_000, "CW"), (50_100_000, 50_300_000, "SSB"),
]
for _mode, _dials in DIGITAL_DIALS.items():
    for _d in _dials:
        SEGMENTS.append((_d, _d + 3_000, _mode))

COLOURS = {"CW": "#2b7de9", "SSB": "#d9480f", "FT8": "#2f9e44", "FT4": "#37b24d",
           "RTTY/data": "#9c36b5"}


def band_for(hz: float | None):
    if not hz:
        return None
    for lo, hi, name in BANDS:
        if lo <= hz <= hi:
            return lo, hi, name
    return None


def segments_between(lo: float, hi: float):
    return [s for s in SEGMENTS if s[1] > lo and s[0] < hi]


def digital_dial_at(hz: float):
    """If `hz` lies in a standard FT8/FT4 slot, return (mode, dial frequency)."""
    for mode, dials in DIGITAL_DIALS.items():
        for d in dials:
            if d - 200 <= hz <= d + 3_200:
                return mode, d
    return None
