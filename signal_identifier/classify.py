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

from .dsp import decimate, noise_floor_db, stft_db, undo_agc

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
        self.fs = fs
        self.seconds = len(x) / fs

        # Long FFT: ~5.9 Hz resolution, good for telling tones apart (FT8, RTTY).
        self.nfft_l = int(2 ** round(np.log2(fs / 5.86)))
        self.hop_l = self.nfft_l // 8
        self.fl = np.fft.rfftfreq(self.nfft_l, 1 / fs)
        self.L = undo_agc(stft_db(x, self.nfft_l, self.hop_l), self.fl)
        self.dt_l = self.hop_l / fs
        self.floor_l = noise_floor_db(self.L, smooth_bins=round(300 / (fs / self.nfft_l)))
        self.act_l = self.L > self.floor_l + 7.0

        # Short FFT: ~5 ms steps, good for keying and loudness changes (CW, voice).
        self.nfft_s = int(2 ** round(np.log2(fs / 47)))
        self.hop_s = self.nfft_s // 4
        self.fs_bins = np.fft.rfftfreq(self.nfft_s, 1 / fs)
        self.S = undo_agc(stft_db(x, self.nfft_s, self.hop_s), self.fs_bins)
        self.dt_s = self.hop_s / fs
        self.floor_s = noise_floor_db(self.S, smooth_bins=7)

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
        band = (self.fl >= F_LO) & (self.fl <= F_HI)
        occ = self.act_l.mean(axis=0)
        occ[~band] = 0
        # Ignore isolated noise speckle: need a few percent of the time active.
        cand = occ >= 0.04
        clusters = [c for c in _clusters(cand, max_gap=3) if self._cluster_ok(*c)]

        dets: list[Detection] = []
        # Speech spreads over hundreds of Hz with gaps between harmonics, so
        # look for it on loosely merged regions before anything else.
        gap = int(120 / self.fl[1])
        remaining = []
        for ra, rb in _clusters(cand, max_gap=gap):
            inside = [c for c in clusters if c[0] >= ra and c[1] <= rb]
            if not inside:
                continue
            a0, b0 = inside[0][0], inside[-1][1]
            f0, f1 = self.fl[a0], self.fl[b0]
            if f1 - f0 >= 350:
                score = self._voice_score(f0, f1)
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
        clusters = remaining

        narrow = []
        for a, b in clusters:
            width = self.fl[b] - self.fl[a] + self.fl[1]
            if width >= 350:
                dets.extend(self._wide(a, b))
            else:
                narrow.append((a, b))

        dets.extend(self._narrow_all(narrow))
        dets = self._drop_shadows(dets)
        dets = self._collapse_unclear(dets)
        dets.sort(key=lambda d: d.freq_hz)
        return dets

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
        ons = []
        for f in (f1, f2):
            env, noise = self.band_envelope(f - 20, f + 20)
            rel = env - noise
            peak = np.percentile(rel, 98)
            ons.append(rel > max(6.0, peak - 12.0))
        either = ons[0] | ons[1]
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
        on = rel > max(6.0, peak - 10.0)
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

        if width <= 90 and longest_blk >= 3 and psk_line:
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
