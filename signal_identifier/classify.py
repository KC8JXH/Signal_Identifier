"""Work out what kind of signal(s) are present in a chunk of receiver audio.

The approach is rule-based, roughly the way an operator reads a waterfall:

* find the places on the waterfall where something is happening;
* for each one, measure how wide it is, whether it keys on and off, whether it
  hops between a fixed set of tones and how long it dwells on each, and whether
  its loudness flutters like speech;
* match those measurements against what each mode is known to look like.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import sstv
from .dsp import decimate, noise_floor_db, passband_mask, stft_db, undo_agc  # noqa: F401

F_LO, F_HI = 150.0, 3500.0
DEBUG = False  # the part of the audio we look at (SSB passband)


@dataclass
class Detection:
    mode: str              # short name, e.g. "FT8"
    freq_hz: float         # centre audio frequency
    bandwidth_hz: float
    confidence: float      # 0..1, a rough guide only
    detail: str = ""       # extra info for the operator, e.g. "about 20 WPM"
    strength_db: float = 0.0  # how far above the noise

    def describe(self) -> str:
        s = f"{self.mode} at {self.freq_hz:,.0f} Hz"
        if self.detail:
            s += f" ({self.detail})"
        return s


@dataclass
class Analysis:
    detections: list[Detection] = field(default_factory=list)
    spectrogram_db: np.ndarray | None = None  # for drawing a waterfall
    freqs_hz: np.ndarray | None = None
    noise_db: np.ndarray | None = None
    seconds: float = 0.0


# ---------------------------------------------------------------- utilities

def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lengths (in frames) of runs of True and runs of False."""
    if len(mask) == 0:
        return np.array([]), np.array([])
    m = mask.astype(np.int8)
    edges = np.flatnonzero(np.diff(m)) + 1
    bounds = np.concatenate([[0], edges, [len(m)]])
    lengths = np.diff(bounds)
    values = m[bounds[:-1]].astype(bool)
    return lengths[values], lengths[~values]


def _clusters(active: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """Group True bins into (start, end_inclusive) clusters, bridging small gaps."""
    idx = np.flatnonzero(active)
    if len(idx) == 0:
        return []
    out = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i - prev > max_gap + 1:
            out.append((start, prev))
            start = i
        prev = i
    out.append((start, prev))
    return out


def _near(value: float, targets, tol: float) -> float | None:
    for t in targets:
        if abs(value - t) <= tol:
            return t
    return None


# ---------------------------------------------------------------- analyser

class Analyzer:
    """Holds the two spectrograms (fine-frequency and fine-time) for one chunk."""

    def __init__(self, audio: np.ndarray, fs: float):
        x, fs = decimate(audio, fs)
        x = x - np.mean(x) if len(x) else x
        self.x = x
        self.fs = fs
        self.seconds = len(x) / fs

        # Long FFT: ~5.9 Hz resolution, good for telling tones apart (FT8, RTTY).
        self.nfft_l = int(2 ** round(np.log2(fs / 5.86)))
        self.hop_l = self.nfft_l // 8
        self.fl = np.fft.rfftfreq(self.nfft_l, 1 / fs)
        raw = stft_db(x, self.nfft_l, self.hop_l)
        # An SSTV picture is loud and never pauses, which would make everything
        # quieter look like it's outside the radio's filter. Find it first and
        # judge the passband from the rest.
        self.picture = sstv.detect(x, fs, (10 ** (raw / 10)).mean(axis=0), self.fl) if len(x) >= fs * 2 else None
        pb_src = raw
        pic = None
        if self.picture is not None:
            pic = (self.fl >= self.picture["lo_hz"] - 30) & (self.fl <= self.picture["hi_hz"] + 30)
            others = (self.fl >= 100) & (self.fl <= 3800) & ~pic
            if others.any():
                pb_src = raw.copy()
                pb_src[:, pic] = np.median(raw[:, others], axis=1, keepdims=True)
        self.pb_l = passband_mask(pb_src, self.fl)
        if pic is not None:
            self.pb_l |= pic & (self.fl >= 100) & (self.fl <= 3800)
        self.L = undo_agc(raw, self.pb_l)
        self.dt_l = self.hop_l / fs
        self.floor_l = noise_floor_db(self.L, round(300 / (fs / self.nfft_l)), self.pb_l)
        self.act_l = (self.L > self.floor_l + 7.0) & self.pb_l[None, :]

        # Short FFT: ~5 ms steps, good for keying and loudness changes (CW, voice).
        self.nfft_s = int(2 ** round(np.log2(fs / 47)))
        self.hop_s = self.nfft_s // 4
        self.fs_bins = np.fft.rfftfreq(self.nfft_s, 1 / fs)
        raw = stft_db(x, self.nfft_s, self.hop_s)
        # Use the fine-frequency view to decide where the filter passband is.
        pb_idx = np.clip(np.searchsorted(self.fl, self.fs_bins), 0, len(self.fl) - 1)
        self.pb_s = self.pb_l[pb_idx]
        self.S = undo_agc(raw, self.pb_s)
        self.dt_s = self.hop_s / fs
        self.floor_s = noise_floor_db(self.S, 7, self.pb_s)

    # -- helpers ------------------------------------------------------------

    def band_envelope(self, f_lo: float, f_hi: float) -> tuple[np.ndarray, float]:
        """Short-time power in a band (dB) and that band's noise level (dB)."""
        sel = (self.fs_bins >= f_lo - 15) & (self.fs_bins <= f_hi + 15)
        if not sel.any():
            sel[np.argmin(np.abs(self.fs_bins - (f_lo + f_hi) / 2))] = True
        p = 10 ** (self.S[:, sel] / 10)
        env = 10 * np.log10(p.sum(axis=1) + 1e-20)
        noise = 10 * np.log10((10 ** (self.floor_s[sel] / 10)).sum())
        return env, noise

    # -- main entry ------------------------------------------------------------

    def run(self) -> list[Detection]:
        self._voice_bands = []
        if self.seconds < 2.0:
            return []
        picture = self._sstv()
        band = (self.fl >= F_LO) & (self.fl <= F_HI)
        occ = self.act_l.mean(axis=0)
        occ[~band] = 0
        if picture is not None:
            # Judge everything else without the picture in the way.
            plo = picture.freq_hz - picture.bandwidth_hz / 2 - 150   # sync pulses splash lower
            phi = picture.freq_hz + picture.bandwidth_hz / 2 + 300   # sharp picture edges splash higher
            occ[(self.fl >= plo) & (self.fl <= phi)] = 0
        # Ignore isolated noise speckle: need a few percent of the time active.
        cand = occ >= 0.04
        clusters = [c for c in _clusters(cand, max_gap=3) if self._cluster_ok(*c)]

        dets: list[Detection] = []
        # Speech spreads over hundreds of Hz with gaps between harmonics, so
        # look for it on loosely merged regions before anything else.
        gap = int(120 / self.fl[1])
        remaining = []
        regions = []
        for ra, rb in _clusters(cand, max_gap=gap):
            inside = [c for c in clusters if c[0] >= ra and c[1] <= rb]
            if not inside:
                continue
            a0, b0 = inside[0][0], inside[-1][1]
            f0, f1 = self.fl[a0], self.fl[b0]
            if f1 - f0 >= 350:
                # Wide data bursts can fool the voice test - but don't let a busy
                # FT8/FT4/CW stretch be swallowed as one "burst".
                familiar = any(d.mode in self.KNOWN and d.confidence >= 0.7
                               for d in self._narrow_all([c for c in inside
                                                          if self.fl[c[1]] - self.fl[c[0]] < 350]))
                d = None if familiar else self._data_bursts(f0, f1, need_fill=True)
                if d is not None:
                    dets.append(d)
                    continue
                score = self._voice_score(f0, f1)
                if score >= 0.5 and self._pumped_by_keying(inside, self._last_voice_env):
                    score = 0.0   # it's a strong CW signal pumping the AGC, not speech
                if score >= 0.5:
                    merged = False
                    for d in dets:
                        if d.mode == "SSB voice":
                            vlo = min(d.freq_hz - d.bandwidth_hz / 2, f0)
                            vhi = max(d.freq_hz + d.bandwidth_hz / 2, f1)
                            d.freq_hz, d.bandwidth_hz = (vlo + vhi) / 2, vhi - vlo
                            d.confidence = max(d.confidence, min(0.95, score))
                            merged = True
                    if not merged:
                        dets.append(Detection("SSB voice", (f0 + f1) / 2, f1 - f0,
                                              min(0.95, score), "someone talking",
                                              self._strength(a0, b0)))
                    self._voice_bands.append((f0, f1, self._last_voice_env))
                    continue
            remaining.extend(inside)
            regions.append((f0, f1))
        clusters = remaining

        narrow = []
        for a, b in clusters:
            width = self.fl[b] - self.fl[a] + self.fl[1]
            if width >= 350:
                dets.extend(self._wide(a, b))
            else:
                narrow.append((a, b))

        dets.extend(self._narrow_all(narrow))
        dets = self._data_regions(dets, regions)
        dets = self._drop_shadows(dets)
        dets = self._drop_pumping_ghosts(dets)
        dets = self._collapse_unclear(dets)
        if picture is not None:
            # An SSTV picture is one signal: drop the bits of it that looked like
            # other modes (data, FT4, PSK, voice...) and show it as one.
            lo = picture.freq_hz - picture.bandwidth_hz / 2 - 150
            hi = picture.freq_hz + picture.bandwidth_hz / 2 + 300
            dets = [d for d in dets if not lo <= d.freq_hz <= hi] + [picture]
        dets.sort(key=lambda d: d.freq_hz)
        return dets

    def _sstv(self) -> Detection | None:
        """SSTV picture or weather fax, found by its steady scan-line rhythm."""
        r = self.picture
        if r is None:
            return None
        fax = r["mode"].startswith("WEFAX")
        detail = r["mode"]
        off = r["offset_hz"]
        if not fax and abs(off) >= 150:
            khz = abs(off) / 1000
            detail += (f", tones {khz:.1f} kHz too low: tune down {khz:.1f} kHz" if off < 0 else
                       f", tones {khz:.1f} kHz too high: tune up {khz:.1f} kHz")
        lo, hi = max(F_LO, r["lo_hz"]), min(F_HI, r["hi_hz"])
        a, b = int(np.searchsorted(self.fl, lo)), int(np.searchsorted(self.fl, hi))
        conf = min(0.95, 0.6 + (r["score"] - sstv.MIN_SCORE))
        return Detection("Weather fax" if fax else "SSTV", (lo + hi) / 2, hi - lo, conf, detail,
                         self._strength(a, max(a + 1, b)))

    KNOWN = ("CW", "FT8", "FT4", "RTTY", "PSK31", "Carrier", "SSB voice", "SSTV", "Weather fax")

    def _data_regions(self, dets: list[Detection], regions) -> list[Detection]:
        """Where nothing familiar was found, check for ARQ-style data bursts
        (Winlink: ARDOP, VARA, PACTOR...) covering the whole region."""
        def near_voice(f):
            return any(lo - 600 <= f <= hi + 600 for lo, hi, _ in self._voice_bands)

        for d in list(dets):
            if d.mode.startswith("Unknown") and d.bandwidth_hz >= 100 and not near_voice(d.freq_hz):
                nd = self._data_bursts(d.freq_hz - d.bandwidth_hz / 2, d.freq_hz + d.bandwidth_hz / 2)
                if nd is not None:
                    dets = [x for x in dets if x is not d] + [nd]
        for f0, f1 in regions:
            if f1 - f0 < 120 or near_voice((f0 + f1) / 2):
                continue
            inside = [d for d in dets if f0 - 60 <= d.freq_hz <= f1 + 60 and d.mode != "SSB voice"]
            if any(d.mode in self.KNOWN and d.confidence >= 0.7 for d in inside):
                continue
            # measure the width from where the signal really is (not its skirts)
            a0, b0 = int(np.searchsorted(self.fl, f0)), int(np.searchsorted(self.fl, f1))
            if b0 > a0:
                a0, b0 = self._core(a0, b0)
                f0, f1 = self.fl[a0], self.fl[b0]
            d = self._data_bursts(f0, f1)
            if d is not None:
                dets = [x for x in dets if x not in inside] + [d]
        return dets

    def _data_bursts(self, f_lo: float, f_hi: float, need_fill: bool = False) -> Detection | None:
        """Blocks of data from a fraction of a second up to several seconds, with
        sharp starts and stops and a steady level while on: the pattern of ARQ
        data links, where stations take turns sending and acknowledging."""
        width = f_hi - f_lo
        if width < 120 or width > 3000:
            return None
        env, noise = self.band_envelope(f_lo, f_hi)
        k = max(1, int(0.03 / self.dt_s))
        env = 10 * np.log10(np.convolve(10 ** (env / 10), np.ones(k) / k, mode="same") + 1e-20)
        # Background = the band's level in the gaps between bursts. (A wide
        # signal that's on most of the time skews the general noise estimate.)
        noise = min(noise, float(np.percentile(env, 8)) + 1.0)
        rel = env - noise
        peak = float(np.percentile(rel, 98))
        if peak < 7:
            return None
        on = rel > max(5.0, peak - 12.0)
        # bridge short dropouts (< 60 ms), then drop blips (< 0.2 s)
        on = self._bridge(on, int(0.06 / self.dt_s))
        runs_on, _ = _runs(on)
        blocks = runs_on[runs_on * self.dt_s >= 0.2] * self.dt_s
        if len(blocks) == 0:
            return None
        if blocks.max() > 9.0 or on.mean() > 0.92:
            return None   # continuous, or FT8-length transmissions: not ARQ bursts
        if len(blocks) < 2 and blocks.max() < 0.5:
            return None
        inside = rel[on]
        if len(inside) < 20 or np.std(inside) > 4.5:
            return None   # level jumps around while on: speech, not data
        steady = np.std(inside) <= 2.5 and blocks.max() >= 0.8 and np.median(inside) >= 6
        if np.median(inside) < 7 and not steady:
            return None   # too faint to judge
        # Where is the signal really? Average spectrum while it's on.
        a = int(np.searchsorted(self.fl, f_lo))
        b = int(np.searchsorted(self.fl, f_hi))
        if b - a >= 3:
            t_long = (np.arange(len(self.L)) * self.hop_l + self.nfft_l / 2) / self.fs
            on_long = on[np.clip((t_long / self.dt_s).astype(int), 0, len(on) - 1)]
            if on_long.sum() >= 3:
                avg = 10 * np.log10(np.mean(10 ** (self.L[on_long, a:b + 1] / 10), axis=0))
                base = float(np.median(self.floor_l[a:b + 1]))
                top = float(avg.max())
                keep = np.flatnonzero(avg >= max(top - 12, base + 0.5 * (top - base)))
                a, b = a + int(keep[0]), a + int(keep[-1])
                f_lo, f_hi = self.fl[a], self.fl[b]
                width = f_hi - f_lo + self.fl[1]
        # how much of the band is lit at once while on (multi-carrier modes fill it)
        sel = (self.fs_bins >= f_lo) & (self.fs_bins <= f_hi)
        lit = (self.S[:, sel] > self.floor_s[sel] + 6)[on].mean() if sel.any() else 0
        # Before the voice test: either many carriers lit at once, or a level so
        # steady while on (< 2.5 dB spread) that it can't be speech.
        if need_fill and lit < 0.5 and not steady:
            return None
        # tone layout, for the detail text
        peaks, spacing = self._tone_layout(a, b)
        if len(peaks) == 2 and _near(abs(peaks[1] - peaks[0]), (170, 200, 425, 850), 15):
            return None   # that's an RTTY pair
        centre = (f_lo + f_hi) / 2
        if width < 300 and 3 <= len(peaks) <= 5 and spacing and 40 <= spacing <= 60:
            detail, conf = f"bursts, {len(peaks)} tones ~50 Hz apart: looks like ARDOP (Winlink)", 0.75
        elif width < 300:
            detail, conf = f"bursts, {width:.0f} Hz wide: ARQ data (ARDOP 200 / PACTOR?)", 0.6
        elif width < 800:
            detail, conf = f"bursts, {width:.0f} Hz wide: ARQ data (VARA 500 / ARDOP 500 / PACTOR?)", 0.6
        else:
            detail, conf = f"bursts, {width:.0f} Hz wide: ARQ data (VARA / PACTOR / ARDOP 2000?)", 0.6
        strength = float(np.percentile(rel, 90))
        return Detection("Data bursts", centre, width, conf, detail, strength)

    @staticmethod
    def _bridge(on: np.ndarray, gap: int) -> np.ndarray:
        on = on.copy()
        m = on.astype(np.int8)
        edges = np.flatnonzero(np.diff(m)) + 1
        bounds = np.concatenate([[0], edges, [len(m)]])
        for i in range(len(bounds) - 1):
            st, en = bounds[i], bounds[i + 1]
            if not on[st] and st > 0 and en < len(on) and en - st <= gap:
                on[st:en] = True
        return on

    def _tone_layout(self, a: int, b: int):
        """Tones a signal hops between: (tone frequencies, typical spacing).

        Looks at where the strongest point sits moment by moment (~40 ms
        steps) and finds the frequencies it keeps returning to. Works for FSK
        modes, where one tone is on at a time.
        """
        f_lo, f_hi = self.fl[a], self.fl[b]
        if f_hi - f_lo < 30 or len(self.x) < 1024:
            return [], None
        nfft = int(2 ** round(np.log2(self.fs / 21.5)))
        from .dsp import stft_db
        S = stft_db(self.x, nfft, nfft // 4)
        f = np.fft.rfftfreq(nfft, 1 / self.fs)
        sel = (f >= f_lo - 10) & (f <= f_hi + 10)
        if sel.sum() < 3:
            return [], None
        sub = S[:, sel]
        fsel = f[sel]
        noise = np.percentile(sub, 20)
        strong = sub.max(axis=1) > noise + 12
        if strong.sum() < 20:
            return [], None
        sr = sub[strong]
        k = np.argmax(sr, axis=1)
        kk = np.clip(k, 1, sr.shape[1] - 2)
        l, c, r = sr[np.arange(len(k)), kk - 1], sr[np.arange(len(k)), kk], sr[np.arange(len(k)), kk + 1]
        den = l - 2 * c + r
        off = np.clip(np.where(np.abs(den) > 1e-9, 0.5 * (l - r) / np.where(den == 0, 1, den), 0), -0.5, 0.5)
        pf = fsel[kk] + off * (f[1] - f[0])
        bins = np.arange(f_lo - 10, f_hi + 15, 5.0)
        h, edges = np.histogram(pf, bins=bins)
        h = np.convolve(h, [1, 2, 1], mode="same")
        top = h.max()
        pk = [i for i in range(1, len(h) - 1) if h[i] >= h[i - 1] and h[i] >= h[i + 1] and h[i] >= 0.25 * top]
        freqs = []
        for i in sorted(pk, key=lambda i: -h[i]):
            fc = edges[i] + 2.5
            if all(abs(fc - g) >= 30 for g in freqs):
                freqs.append(fc)
        freqs.sort()
        spacing = float(np.median(np.diff(freqs))) if len(freqs) >= 2 else None
        return freqs, spacing

    def _collapse_unclear(self, dets: list[Detection]) -> list[Detection]:
        """Several faint, unsure guesses at once usually means one weak, noisy
        signal (often a faint voice) rather than many different ones. Say so
        honestly instead of listing a pile of guesses."""
        unclear = [d for d in dets if d.confidence < 0.8 and d.strength_db < 14]
        if len(unclear) < 3:
            return dets
        lo = min(d.freq_hz - d.bandwidth_hz / 2 for d in unclear)
        hi = max(d.freq_hz + d.bandwidth_hz / 2 for d in unclear)
        keep = [d for d in dets if d not in unclear]
        keep.append(Detection("Faint signal", (lo + hi) / 2, hi - lo, 0.3,
                              "too weak or noisy to be sure, possibly weak voice",
                              max(d.strength_db for d in unclear)))
        return keep

    def _drop_pumping_ghosts(self, dets: list[Detection]) -> list[Detection]:
        """Faint 'signals' that rise and fall exactly with a much stronger CW
        signal's keying are the AGC pumping the background, not real signals."""
        strong = [d for d in dets if d.mode == "CW" and d.strength_db >= 25]
        if not strong:
            return dets
        k = max(1, int(0.03 / self.dt_s))
        keep = []
        for d in dets:
            ghost = False
            for c in strong:
                if d is c or c.strength_db - d.strength_db < 15:
                    continue
                if c.strength_db - d.strength_db >= 35 and d.confidence < 0.8:
                    ghost = True   # unsure and far weaker: AGC squeeze leftovers
                    continue
                e1, _ = self.band_envelope(c.freq_hz - c.bandwidth_hz / 2, c.freq_hz + c.bandwidth_hz / 2)
                e2, _ = self.band_envelope(d.freq_hz - d.bandwidth_hz / 2, d.freq_hz + d.bandwidth_hz / 2)
                e1 = np.convolve(e1, np.ones(k) / k, mode="same")
                e2 = np.convolve(e2, np.ones(k) / k, mode="same")
                if np.std(e1) > 0 and np.std(e2) > 0 and abs(np.corrcoef(e1, e2)[0, 1]) > 0.3:
                    ghost = True
            if not ghost:
                keep.append(d)
        return keep

    def _drop_shadows(self, dets: list[Detection]) -> list[Detection]:
        """Remove weak 'ghosts' sitting right next to a much stronger signal
        (sidebands, splatter, filter skirts)."""
        keep = []
        for d in dets:
            ghost = False
            for o in dets:
                if o is d:
                    continue
                dist = abs(o.freq_hz - d.freq_hz)
                diff = o.strength_db - d.strength_db
                if dist <= 250 + o.bandwidth_hz / 2 and diff >= 18:
                    ghost = True
                if dist <= 150 + o.bandwidth_hz / 2 and diff >= 10 and d.mode not in ("SSB voice",):
                    ghost = True
            if not ghost:
                keep.append(d)
        return keep

    def _cluster_ok(self, a: int, b: int) -> bool:
        # Must be clearly above noise somewhere, and active for at least ~0.5 s.
        sub = self.act_l[:, a:b + 1]
        frames_active = sub.any(axis=1).sum()
        return frames_active * self.dt_l >= 0.5

    def _strength(self, a: int, b: int) -> float:
        sub = self.L[:, a:b + 1] - self.floor_l[a:b + 1]
        return float(np.percentile(sub.max(axis=1), 90))

    # -- wide signals: voice, or a crowd of digital signals ------------------

    def _wide(self, a: int, b: int) -> list[Detection]:
        f_lo, f_hi = self.fl[a], self.fl[b]
        # Not speech: probably several digital signals side by side (e.g. a busy
        # FT8 band). Split on the quieter gaps between them and classify each.
        occ = self.act_l[:, a:b + 1].mean(axis=0)
        thresh = max(0.04, 0.35 * np.percentile(occ, 75))
        subs = [(a + s, a + e) for s, e in _clusters(occ >= thresh, max_gap=1)]
        subs = [(s, e) for s, e in subs
                if self.fl[e] - self.fl[s] < 350 and self._cluster_ok(s, e)]
        if not subs:
            return [Detection("Unknown wide signal", (f_lo + f_hi) / 2, f_hi - f_lo,
                              0.3, "", self._strength(a, b))]
        return self._narrow_all(subs)

    def _voice_score(self, f_lo: float, f_hi: float) -> float:
        """0..1: how much the loudness in this band behaves like speech.

        Speech comes in syllables, so the whole band gets louder and quieter
        together several times a second. A crowd of digital signals stays at a
        steady level instead.
        """
        # Count how many spots in each third of the band light up at each moment.
        edges = np.linspace(f_lo, f_hi, 4)
        lit = self.S > self.floor_s + 6.0
        k = max(1, int(0.03 / self.dt_s))  # smooth over ~30 ms
        kern = np.ones(k) / k
        counts, expected = [], 0.0
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = (self.fs_bins >= lo) & (self.fs_bins < hi)
            c = lit[:, sel].sum(axis=1).astype(float)
            counts.append(np.convolve(c, kern, mode="same"))
            expected += 0.02 * sel.sum()  # noise alone lights ~2% of spots
        counts = np.array(counts)
        total = counts.sum(axis=0)
        self._last_voice_env = total
        active = total > 3 * expected + 1.5
        if active.mean() < 0.1:
            return 0.0
        # Speech starts and stops several times a second (syllables, words).
        # Digital signals switch on and off rarely.
        hop = max(1, int(0.02 / self.dt_s))
        coarse = active[::hop]
        on_runs, _ = _runs(coarse)
        syllables = np.count_nonzero(on_runs >= 2) / self.seconds
        if syllables < 0.8:
            return 0.0
        # Do the low, middle and high parts rise and fall together (one talker)?
        w = max(3, int(1.0 / self.dt_s))
        hp = np.array([c - np.convolve(c, np.ones(w) / w, mode="same") for c in counts])
        if np.any(hp.std(axis=1) == 0):
            return 0.0
        cc = np.corrcoef(hp)
        corr = float(np.mean([cc[0, 1], cc[0, 2], cc[1, 2]]))
        if corr < 0.2:
            return 0.0
        score = 0.5 * np.clip((syllables - 0.8) / 1.5, 0, 1) + 0.5 * np.clip((corr - 0.1) / 0.4, 0, 1)
        return float(score)

    # -- narrow signals --------------------------------------------------------

    def _narrow_all(self, clusters: list[tuple[int, int]]) -> list[Detection]:
        dets = []
        used = set()
        # RTTY first: two separate tones at a standard shift, both busy at the same times.
        info = []
        clusters = [self._core(a, b) for a, b in clusters]
        clusters = [c for c in clusters if not self._part_of_voice(*c)]
        for a, b in clusters:
            on = self.act_l[:, a:b + 1].any(axis=1)
            occ = self.act_l[:, a:b + 1].mean(axis=0).max()
            centre = self._centre(a, b)
            info.append((a, b, centre, on, occ, self._wander(a, b)))
        for i in range(len(info)):
            for j in range(i + 1, len(info)):
                if i in used or j in used:
                    continue
                ai, bi, ci, oni, occi, wi = info[i]
                aj, bj, cj, onj, occj, wj = info[j]
                if self.fl[bi] - self.fl[ai] > 70 or self.fl[bj] - self.fl[aj] > 70:
                    continue
                if wi > 15 or wj > 15:  # an FT8 signal hops ~45 Hz; RTTY tones stay put
                    continue
                shift = abs(cj - ci)
                std = _near(shift, (170, 200, 425, 850), tol=0.08 * shift + 8)
                if std is None or min(occi, occj) < 0.2:
                    continue
                both = (oni & onj).sum()
                either = (oni | onj).sum()
                if either == 0 or both / either < 0.4:
                    continue
                if not self._always_one_on(ci, cj):
                    continue
                used.update((i, j))
                lo, hi = sorted((ci, cj))
                conf = 0.85 if std == 170 else 0.7
                dets.append(Detection("RTTY", (lo + hi) / 2, hi - lo + 30, conf,
                                      f"{std} Hz shift, mark/space {lo:.0f}/{hi:.0f} Hz",
                                      max(self._strength(ai, bi), self._strength(aj, bj))))
        for k, (a, b, *_rest) in enumerate(info):
            if k not in used:
                d = self._narrow(a, b)
                if d is not None:
                    dets.append(d)
        return dets

    def _pumped_by_keying(self, clusters, voice_env: np.ndarray) -> bool:
        """True if the 'syllables' follow the on/off keying of a strong narrow
        signal in the same region (AGC pumping or key clicks from CW)."""
        best = None
        for a, b in clusters:
            a, b = self._core(a, b)
            if self.fl[b] - self.fl[a] > 60:
                continue
            st = self._strength(a, b)
            if best is None or st > best[0]:
                best = (st, a, b)
        if best is None or best[0] < 20:
            return False
        _, a, b = best
        env, noise = self.band_envelope(self.fl[a], self.fl[b])
        if np.std(env) == 0 or np.std(voice_env) == 0:
            return False
        k = max(1, int(0.03 / self.dt_s))
        e = np.convolve(env, np.ones(k) / k, mode="same")
        v = np.convolve(voice_env, np.ones(k) / k, mode="same")
        c = np.corrcoef(e, v)[0, 1]
        return abs(c) > 0.3

    def _part_of_voice(self, a: int, b: int) -> bool:
        """True if this bit of the waterfall rises and falls with nearby speech
        (a stray harmonic of the talker, not a separate signal)."""
        f = (self.fl[a] + self.fl[b]) / 2
        occ = self.act_l[:, a:b + 1].mean(axis=0).max()
        for lo, hi, venv in self._voice_bands:
            if lo - 800 <= f <= hi + 800:
                # Faint, rarely-lit patches beside a talker are their harmonics.
                if occ < 0.2:
                    return True
                env, noise = self.band_envelope(self.fl[a], self.fl[b])
                if np.std(env) > 0 and np.std(venv) > 0 and np.corrcoef(env, venv)[0, 1] > 0.3:
                    return True
        return False

    def _core(self, a: int, b: int) -> tuple[int, int]:
        """Trim a cluster to where the signal itself is (within 15 dB of its
        strongest part), ignoring key clicks and filter skirts."""
        sub = self.L[:, a:b + 1]
        rows = self.act_l[:, a:b + 1].any(axis=1)
        if rows.sum() == 0:
            return a, b
        avg = 10 * np.log10(np.mean(10 ** (sub[rows] / 10), axis=0))
        keep = np.flatnonzero(avg >= avg.max() - 15)
        return a + int(keep[0]), a + int(keep[-1])

    def _strong_rows(self, a: int, b: int) -> np.ndarray:
        """Moments when this signal is clearly present (within 6 dB of its usual peak)."""
        pk = (self.L[:, a:b + 1] - self.floor_l[a:b + 1]).max(axis=1)
        act = self.act_l[:, a:b + 1].any(axis=1)
        if act.sum() == 0:
            return act
        return act & (pk >= np.percentile(pk[act], 90) - 6)

    def _always_one_on(self, f1: float, f2: float) -> bool:
        """RTTY: while transmitting, one of the two tones is always on. Two
        separate CW stations both go quiet in their gaps."""
        total = 0.0
        noise_total = 0.0
        for f in (f1, f2):
            env, noise = self.band_envelope(f - 20, f + 20)
            total = total + 10 ** (env / 10)
            noise_total += 10 ** (noise / 10)
        rel = 10 * np.log10(total + 1e-20) - 10 * np.log10(noise_total)
        k = max(1, int(0.03 / self.dt_s))
        rel = 10 * np.log10(np.convolve(10 ** (rel / 10), np.ones(k) / k, mode="same") + 1e-20)
        peak = np.percentile(rel, 98)
        either = rel > max(6.0, peak - 12.0)
        idx = np.flatnonzero(either)
        if len(idx) < 10:
            return False
        span = either[idx[0]:idx[-1] + 1]
        return span.mean() >= 0.85

    def _wander(self, a: int, b: int) -> float:
        """How far (Hz) the strongest point moves around inside the cluster."""
        rows = self._strong_rows(a, b)
        if rows.sum() < 5:
            return 0.0
        pf = self.fl[a + np.argmax(self.L[rows, a:b + 1], axis=1)]
        return float(np.percentile(pf, 90) - np.percentile(pf, 10))

    def _centre(self, a: int, b: int) -> float:
        w = self.act_l[:, a:b + 1].mean(axis=0) + 1e-9
        return float(np.sum(self.fl[a:b + 1] * w) / np.sum(w))

    def _narrow(self, a: int, b: int) -> Detection | None:
        df = self.fl[1]
        width = self.fl[b] - self.fl[a] + df
        centre = self._centre(a, b)
        strength = self._strength(a, b)

        # Loudness over time with ~5 ms steps (keying, phase reversals).
        env, noise = self.band_envelope(self.fl[a], self.fl[b])
        rel = env - noise
        k = max(1, int(0.010 / self.dt_s))
        rel = 10 * np.log10(np.convolve(10 ** (rel / 10), np.ones(k) / k, mode="same") + 1e-20)
        peak = np.percentile(rel, 98)
        if peak < 6:
            return None
        # Key-down threshold: halfway between noise and the signal's peak (but no
        # more than 10 dB down), so AGC overshoot on strong signals doesn't
        # shorten every dot and dash.
        on = rel > max(6.0, min(peak - 10.0, peak / 2))
        # Bridge tiny dropouts (key clicks, noise) shorter than ~10 ms.
        gap = max(1, int(0.010 / self.dt_s))
        on_runs0, off_runs0 = _runs(on)
        if len(off_runs0):
            m = on.astype(np.int8)
            edges = np.flatnonzero(np.diff(m)) + 1
            bounds = np.concatenate([[0], edges, [len(m)]])
            for i in range(len(bounds) - 1):
                st, en = bounds[i], bounds[i + 1]
                if not on[st] and st > 0 and en < len(on) and en - st <= gap:
                    on[st:en] = True
        on_frac = on.mean()
        on_runs, off_runs = _runs(on)
        # Drop tiny glitches (<2 steps) when counting key-downs.
        on_runs = on_runs[on_runs >= 2]
        trans_per_s = len(on_runs) / self.seconds

        # Long-term transmission blocks (gaps over 1 s split them).
        blk_on = self.act_l[:, a:b + 1].any(axis=1)
        blk = np.convolve(blk_on.astype(float), np.ones(int(1.0 / self.dt_l)), mode="same") > 0
        blk_runs, _ = _runs(blk)
        longest_blk = (blk_runs.max() * self.dt_l - 1.0) if len(blk_runs) else 0.0

        # Tone hopping: where in the cluster is the peak, frame by frame?
        sub = self.L[:, a:b + 1]
        rows = self._strong_rows(a, b)
        if rows.sum() >= 5:
            sr = sub[rows]
            pk = np.argmax(sr, axis=1)
            # parabolic interpolation for sub-bin peak frequency
            l = sr[np.arange(len(pk)), np.clip(pk - 1, 0, sr.shape[1] - 1)]
            c = sr[np.arange(len(pk)), pk]
            r = sr[np.arange(len(pk)), np.clip(pk + 1, 0, sr.shape[1] - 1)]
            den = l - 2 * c + r
            off = np.where(np.abs(den) > 1e-9, 0.5 * (l - r) / np.where(den == 0, 1, den), 0)
            off = np.clip(off, -0.5, 0.5)
            pf = self.fl[a] + (pk + off) * df
            span = float(np.percentile(pf, 95) - np.percentile(pf, 5))
            same = np.abs(np.diff(pf)) <= 2.5
            dwell_runs, _ = _runs(same)
            dwell = (np.median(dwell_runs) + 1) * self.dt_l if len(dwell_runs) else 0.0
            levels = len(np.unique(np.round((pf - pf.min()) / 6.25)))
        else:
            span, dwell, levels = 0.0, 0.0, 1

        # Loudness steadiness while keyed (PSK31 dips at every phase reversal).
        on_vals = rel[on]
        env_spread = float(np.percentile(on_vals, 90) - np.percentile(on_vals, 10)) if on.sum() > 10 else 0.0
        psk_line = self._psk_rhythm(rel, on)

        self.last_metrics = dict(width=width, span=span, on_frac=on_frac, trans=trans_per_s,
                                 dwell=dwell, levels=levels, blk=longest_blk,
                                 env_spread=env_spread, psk=psk_line)
        if DEBUG:
            print(f"   [{centre:.0f} Hz] " + " ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                                                 for k, v in self.last_metrics.items()))

        # ---- rules -----------------------------------------------------------
        if width <= 45 and span <= 15 and 0.08 <= on_frac <= 0.88 and trans_per_s >= 1.0:
            on_ms = on_runs * self.dt_s * 1000
            dit = float(np.percentile(on_ms, 25)) if len(on_ms) else 0
            detail = ""
            if 25 <= dit <= 400:
                detail = f"about {1200 / dit:.0f} WPM"
            conf = 0.9 if 0.25 <= on_frac <= 0.7 else 0.7
            return Detection("CW", centre, width, conf, detail, strength)

        if width <= 90 and longest_blk >= 3 and psk_line and strength >= 12:
            return Detection("PSK31", centre, max(width, 31), 0.75, "", strength)

        if 25 <= width <= 75 and 0.08 <= dwell <= 0.3 and levels >= 4 and env_spread < 8:
            conf = 0.85 if 11.0 <= longest_blk <= 14.5 else 0.7
            return Detection("FT8", centre, 50, conf, "", strength)

        if 55 <= width <= 120 and levels >= 3 and (longest_blk <= 7.5 or dwell < 0.08) and env_spread < 8:
            conf = 0.75 if 4.5 <= longest_blk <= 7.5 else 0.55
            return Detection("FT4", centre, 83, conf, "", strength)

        if 75 <= width <= 350 and 0.08 <= dwell <= 0.3 and 11.0 <= longest_blk <= 14.5 and env_spread < 8:
            n = max(2, round(width / 55))
            return Detection("FT8", centre, width, 0.7, f"about {n} signals overlapping", strength)

        if width <= 30 and on_frac > 0.88 and span <= 12 and env_spread < 6:
            return Detection("Carrier", centre, width, 0.7,
                             "steady tone: tuning, beacon, or AM carrier", strength)

        if 25 <= width <= 75 and levels >= 4:
            return Detection("FT8", centre, 50, 0.5, "best guess", strength)

        return Detection("Unknown", centre, width, 0.3,
                         f"{width:.0f} Hz wide digital or data signal", strength)

    def _psk_rhythm(self, rel: np.ndarray, on: np.ndarray) -> bool:
        """PSK31 loudness dips at multiples of 1/31.25 s (phase reversals)."""
        if len(rel) < 200:
            return False
        lin = 10 ** (rel / 20)
        lin = lin - lin.mean()
        spec = np.abs(np.fft.rfft(lin * np.hanning(len(lin))))
        f = np.fft.rfftfreq(len(lin), self.dt_s)
        near = (f > 29) & (f < 33.5)
        ref = (f > 5) & (f < 60) & ~near
        if not near.any() or not ref.any():
            return False
        return spec[near].max() > 5 * np.median(spec[ref])


def analyze(audio: np.ndarray, fs: float, keep_spectrogram: bool = True) -> Analysis:
    """Analyse audio samples and return what was found."""
    an = Analyzer(audio, fs)
    dets = an.run()
    out = Analysis(detections=dets, seconds=an.seconds)
    if keep_spectrogram:
        band = an.fl <= F_HI
        out.spectrogram_db = an.L[:, band]
        out.freqs_hz = an.fl[band]
        out.noise_db = an.floor_l[band]
    return out
