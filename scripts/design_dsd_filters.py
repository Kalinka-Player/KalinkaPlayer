#!/usr/bin/env python3
"""Designs the renderer's DSD-to-PCM decimation filters and writes them out as
a C++ header, with a report of what each configuration achieves.

The chain for a DSD rate M x base and a PCM rate P x base, R = M / P >= 8:

1. Stage 1 divides by 8 at the one-bit rate, one byte at a time, from integer
   tables. Its taps are symmetric, sum to 2^36, and every one of the 8 phases
   sums to 2^33, so the idle pattern 0x69 comes out as exact zero.
2. Each further factor of 2 is a half-band FIR.
3. At the output rate a band filter applies the ultrasonic cut-off, unless
   the filter is "wide".

Every decimator keeps whatever would fold below the band filter's stop edge
(or below the wide passband) far down: STAGE1_DB for stage 1, HALFBAND_DB for
the half-bands. FOLD_DB, the acceptance figure, is far looser; what sets these
is a DSD128 fixture's THD+N, near -173 dB, which the converter must not raise
by 0.1 dB against DSD's near full-scale ultrasonic noise. All filters are
linear phase, from Kaiser-windowed sinc.

Usage: design_dsd_filters.py OUTPUT_HEADER
"""

import math
import sys

import numpy as np

FAMILIES = (44100, 48000)
DSD_MULTIPLES = (64, 128, 256, 512, 1024)
PCM_MULTIPLES = (2, 4, 8)
FILTERS = ("30k", "50k", "wide")

FOLD_DB = 130.0
STAGE1_DB = 170.0
HALFBAND_DB = 162.0
DESIGN_MARGIN_DB = 3.0
BAND_DB = 110.0
PASSBAND_HZ = 20000.0
RIPPLE_DB = 0.005
# Taps 2^-36 apart keep stage 1's rounding near -200 dB; every sum stays an
# integer under 2^53, exact in a double.
STAGE1_SHIFT = 36
# Half the groups are stored, by symmetry, 2 KB each: 30 keeps them in 32 KB.
STAGE1_MAX_GROUPS = 30
WIDE_FRACTION = 0.45

# Band filter: flat to the first edge, stopped from the second.
BANDS = {"30k": (20000.0, 40000.0), "50k": (40000.0, 60000.0)}


def beta(atten_db):
    return 0.1102 * (atten_db - 8.7)


def kaiser_lowpass(taps, cutoff, rate, atten_db):
    t = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff / rate * np.sinc(2 * cutoff / rate * t)
    return h * np.kaiser(taps, beta(atten_db))


def response_db(h, freqs, rate):
    n = np.arange(len(h))
    w = 2 * np.pi * np.asarray(freqs) / rate
    mag = np.abs(np.exp(-1j * np.outer(w, n)) @ h)
    return 20 * np.log10(np.maximum(mag, 1e-30))


def grid(low, high, points=400):
    return np.linspace(low, high, points) if high > low else np.array([low])


def stage1_stopbands(protect, rate):
    """Where content folds below `protect` once the stage divides by 8."""
    step = rate / 8
    bands = []
    for k in range(1, 5):
        low, high = k * step - protect, min(k * step + protect, rate / 2)
        if low < rate / 2:
            bands.append((low, high))
    return bands


def quantise_stage1(h):
    scale = 1 << STAGE1_SHIFT
    target = scale // 8
    h = h / h.sum() * scale
    # The least change that evens the phases out: its spectrum sits on the
    # multiples of fs/8, where the zeros are wanted.
    for phase in range(8):
        h[phase::8] += (target - h[phase::8].sum()) / len(h[phase::8])
    q = np.round(h).astype(np.int64)
    n = len(q)
    centre = n // 2
    for phase in range(4):
        # The pair nearest the centre in this phase; its mirror is phase 7-p.
        k = max(i for i in range(centre) if i % 8 == phase)
        d = target - q[phase::8].sum()
        q[k] += d
        q[n - 1 - k] += d
    assert all(q[p::8].sum() == target for p in range(8))
    assert np.array_equal(q, q[::-1])
    assert np.abs(q).sum() < 1 << 53
    return q


def design_stage1(protect, rate, max_groups=STAGE1_MAX_GROUPS):
    """The shortest stage 1, in whole bytes of taps, that meets FOLD_DB."""
    bands = stage1_stopbands(protect, rate)
    freqs = np.concatenate([grid(a, b) for a, b in bands])
    goal = STAGE1_DB + DESIGN_MARGIN_DB
    for groups in range(4, max_groups + 1):
        taps = groups * 8
        h = kaiser_lowpass(taps, rate / 16, rate, goal)
        q = quantise_stage1(h)
        worst = response_db(q / float(1 << STAGE1_SHIFT), freqs, rate).max()
        if worst <= -goal:
            return q
    raise RuntimeError(f"no stage 1 for {protect} Hz at {rate} Hz")


def design_halfband(protect, rate):
    """The shortest half-band dividing `rate` by 2 that meets FOLD_DB."""
    freqs = grid(rate / 2 - protect, rate / 2)
    goal = HALFBAND_DB + DESIGN_MARGIN_DB
    for k in range(1, 200):
        taps = 4 * k + 3
        h = kaiser_lowpass(taps, rate / 4, rate, goal)
        centre = taps // 2
        side = h[centre + 1 :: 2]
        side = side * (0.25 / side.sum())
        full = np.zeros(taps)
        full[centre] = 0.5
        full[centre + 1 :: 2] = side
        full[centre - 1 :: -2] = side
        if response_db(full, freqs, rate).max() <= -goal:
            return full
    raise RuntimeError(f"no half-band for {protect} Hz at {rate} Hz")


def design_band(passband, stopband, rate):
    """The shortest band filter at the output rate that meets BAND_DB."""
    freqs = grid(stopband, rate / 2)
    goal = BAND_DB + DESIGN_MARGIN_DB
    for half in range(4, 400):
        taps = 2 * half + 1
        h = kaiser_lowpass(taps, (passband + stopband) / 2, rate, goal)
        h /= h.sum()
        if response_db(h, freqs, rate).max() <= -goal:
            return h
    raise RuntimeError(f"no band filter for {passband} Hz at {rate} Hz")


def widest_stage1_protect(rate):
    """The widest band stage 1 protects within STAGE1_MAX_GROUPS."""
    low, high = 0.0, rate / 16
    for _ in range(30):
        mid = (low + high) / 2
        try:
            design_stage1(mid, rate)
            fits = True
        except RuntimeError:
            fits = False
        low, high = (mid, high) if fits else (low, mid)
    return math.floor(low / 100) * 100


class Designs:
    """Deduplicated filters, keyed by what normalised spec they meet."""

    def __init__(self):
        self.stage1, self.halfbands, self.bands = [], [], []
        self.keys = {}

    def get(self, kind, key, make):
        store = getattr(self, kind)
        full = (kind,) + key
        if full not in self.keys:
            self.keys[full] = len(store)
            store.append(make())
        return self.keys[full]


def chain_for(designs, base, dsd, pcm, name):
    rate_in, rate_out = dsd * base, pcm * base
    ratio = dsd // pcm
    band = None
    if name != "wide" and sum(BANDS[name]) / 2 <= WIDE_FRACTION * rate_out:
        band = BANDS[name]
    if band:
        protect = min(band[1], WIDE_FRACTION * rate_out)
    else:
        protect = WIDE_FRACTION * rate_out
    if ratio == 8:
        protect = min(protect, widest_stage1_protect(rate_in))
    norm = protect / rate_in
    s1 = designs.get(
        "stage1", (round(norm, 9),), lambda: design_stage1(norm * 1e6, 1e6)
    )
    halves = []
    rate = rate_in / 8
    while rate > rate_out:
        r_norm = protect / rate
        halves.append(
            designs.get(
                "halfbands",
                (round(r_norm, 9),),
                lambda r=r_norm: design_halfband(r * 1e6, 1e6),
            )
        )
        rate /= 2
    b = None
    if band:
        key = (band, base, pcm)
        b = designs.get(
            "bands", key, lambda: design_band(band[0], band[1], rate_out)
        )
    return dict(
        base=base, dsd=dsd, pcm=pcm, name=name, protect=protect, stage1=s1,
        halfbands=halves, band=b, passband=band[0] if band else WIDE_FRACTION * rate_out,
    )


def composite_db(designs, chain, freqs):
    """The chain's response at the one-bit rate, before its final decimation."""
    rate = chain["dsd"] * chain["base"]
    total = response_db(
        designs.stage1[chain["stage1"]] / float(1 << STAGE1_SHIFT), freqs, rate
    )
    stage_rate = rate / 8
    for index in chain["halfbands"]:
        total = total + response_db(designs.halfbands[index], freqs, stage_rate)
        stage_rate /= 2
    return total


def check(designs, chain):
    rate_in = chain["dsd"] * chain["base"]
    rate_out = chain["pcm"] * chain["base"]
    protect = chain["protect"]
    alias = []
    j = 1
    while j * rate_out - protect < rate_in / 2:
        alias.append(grid(max(j * rate_out - protect, 0), min(j * rate_out + protect, rate_in / 2), 200))
        j += 1
    fold = composite_db(designs, chain, np.concatenate(alias)).max()
    passband = grid(0, PASSBAND_HZ, 400)
    level = composite_db(designs, chain, passband)
    if chain["band"] is not None:
        level = level + response_db(designs.bands[chain["band"]], passband, rate_out)
    ripple = np.abs(level).max()
    return fold, ripple


def cpp_array(kind, name, values, width=4):
    items = [kind(v) for v in values]
    lines = []
    for i in range(0, len(items), width):
        lines.append("    " + ", ".join(items[i : i + width]) + ",")
    return "\n".join(lines)


def main(path):
    designs = Designs()
    chains = []
    for base in FAMILIES:
        for dsd in DSD_MULTIPLES:
            for pcm in PCM_MULTIPLES:
                if dsd // pcm < 8:
                    continue
                for name in FILTERS:
                    chains.append(chain_for(designs, base, dsd, pcm, name))

    print("| Chain | Stage 1 taps | Half-band taps | Band taps | Protected | Fold | Ripple 0-20 kHz |")
    print("|---|---|---|---|---|---|---|")
    for c in chains:
        fold, ripple = check(designs, c)
        if fold > -FOLD_DB or ripple > RIPPLE_DB:
            raise RuntimeError(f"chain misses its spec: {c} fold {fold} ripple {ripple}")
        s1 = len(designs.stage1[c["stage1"]])
        hb = [len(designs.halfbands[i]) for i in c["halfbands"]]
        band = len(designs.bands[c["band"]]) if c["band"] is not None else 0
        print(
            f"| DSD{c['dsd']} {c['base']} to x{c['pcm']} {c['name']} | {s1} | {hb} | {band} "
            f"| {c['protect'] / 1000:.1f} kHz | {fold:.1f} dB | {ripple:.6f} dB |"
        )

    out = []
    out.append("// Generated by scripts/design_dsd_filters.py; change that, not this.")
    out.append("#pragma once")
    out.append("")
    out.append("#include <cstddef>")
    out.append("#include <cstdint>")
    out.append("")
    out.append("namespace dsd_filters {")
    out.append("")
    out.append(f"inline constexpr int kStage1Shift = {STAGE1_SHIFT};")
    out.append("")
    for i, q in enumerate(designs.stage1):
        out.append(f"inline constexpr int64_t kStage1_{i}[] = {{")
        out.append(cpp_array(str, "", [int(v) for v in q], 6))
        out.append("};")
    for i, h in enumerate(designs.halfbands):
        side = h[len(h) // 2 + 1 :: 2]
        out.append(f"inline constexpr double kHalfBand_{i}[] = {{")
        out.append(cpp_array(lambda v: repr(float(v)), "", side, 3))
        out.append("};")
    for i, h in enumerate(designs.bands):
        half = h[: len(h) // 2 + 1]
        out.append(f"inline constexpr double kBand_{i}[] = {{")
        out.append(cpp_array(lambda v: repr(float(v)), "", half, 3))
        out.append("};")
    out.append("")
    out.append("struct TapsInt {")
    out.append("  const int64_t *taps;")
    out.append("  size_t size;")
    out.append("};")
    out.append("struct Taps64 {")
    out.append("  const double *taps;")
    out.append("  size_t size;")
    out.append("};")
    out.append("")
    out.append("/// Stage 1, whole taps.")
    out.append("inline constexpr TapsInt kStage1[] = {")
    for i in range(len(designs.stage1)):
        out.append(f"    {{kStage1_{i}, std::size(kStage1_{i})}},")
    out.append("};")
    out.append("/// Half-bands: the taps at odd offsets 1, 3, 5... from the centre,")
    out.append("/// whose own tap is 0.5.")
    out.append("inline constexpr Taps64 kHalfBand[] = {")
    for i in range(len(designs.halfbands)):
        out.append(f"    {{kHalfBand_{i}, std::size(kHalfBand_{i})}},")
    out.append("};")
    out.append("/// Band filters: the first half of the taps, the centre last.")
    out.append("inline constexpr Taps64 kBand[] = {")
    for i in range(len(designs.bands)):
        out.append(f"    {{kBand_{i}, std::size(kBand_{i})}},")
    out.append("};")
    out.append("")
    out.append("inline constexpr int kNone = -1;")
    out.append("inline constexpr size_t kMaxHalfBands = 7;")
    out.append("")
    out.append("/// One DSD rate to one PCM rate with one filter; filter 0 is 30k,")
    out.append("/// 1 is 50k, 2 is wide.")
    out.append("struct Chain {")
    out.append("  unsigned base;")
    out.append("  unsigned dsdMultiple;")
    out.append("  unsigned pcmMultiple;")
    out.append("  int filter;")
    out.append("  int stage1;")
    out.append("  int halfBands[kMaxHalfBands];")
    out.append("  int band;")
    out.append("  double passbandHz;")
    out.append("};")
    out.append("")
    out.append("inline constexpr Chain kChains[] = {")
    for c in chains:
        halves = c["halfbands"] + [None] * (7 - len(c["halfbands"]))
        hb = ", ".join("kNone" if h is None else str(h) for h in halves)
        band = "kNone" if c["band"] is None else str(c["band"])
        out.append(
            f"    {{{c['base']}, {c['dsd']}, {c['pcm']}, {FILTERS.index(c['name'])}, "
            f"{c['stage1']}, {{{hb}}}, {band}, {c['passband']:.1f}}},"
        )
    out.append("};")
    out.append("")
    out.append("} // namespace dsd_filters")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
