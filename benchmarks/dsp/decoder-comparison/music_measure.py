"""Analyse two or three 176400 Hz interleaved stereo little-endian float64 files.
Order: Kalinka 30 kHz, optionally Saracon, then FFmpeg. Only the first 52 seconds are needed.
Run in a writable directory. Writes music-bands.csv and music-spectrum.csv.
"""

import argparse
import csv
from pathlib import Path

import numpy as np
from scipy import signal

FS = 176400
NFFT = 16384
START = 30 * FS
LENGTH = 10 * FS
NAMES = ("Kalinka", "Saracon", "FFmpeg")
BANDS = ((20, 20000), (20000, 30000), (30000, 40000), (40000, 80000))
SOS = signal.butter(4, [200, 2000], fs=FS, btype="bandpass", output="sos")


def alignment(reference, candidate, second, channel):
    center = second * FS
    margin = FS // 2
    # Guard samples avoid the filter's edge transients in the search template.
    ref = signal.sosfiltfilt(
        SOS, reference[center - 2048 : center + FS + 2048, channel]
    )[2048:-2048]
    test = signal.sosfiltfilt(
        SOS, candidate[center - margin : center + FS + margin, channel]
    )
    cross = signal.correlate(test, ref, mode="valid", method="fft")
    integral = np.r_[0.0, np.cumsum(test**2)]
    norm = np.sqrt(np.sum(ref**2) * (integral[len(ref) :] - integral[: -len(ref)]))
    normalized = cross / norm
    pos = np.argmax(normalized)
    aligned = test[pos : pos + len(ref)]
    corr = cross[pos] / np.sqrt(np.sum(aligned**2) * np.sum(ref**2))
    return int(pos - margin), corr


def main(paths):
    if len(paths) not in (2, 3):
        raise ValueError("Supply Kalinka, optionally Saracon, then FFmpeg.")
    names = NAMES if len(paths) == 3 else ("Kalinka", "FFmpeg")
    raw = [np.memmap(p, dtype="<f8", mode="r").reshape(-1, 2) for p in paths]
    levels = []
    psds = []
    for name, x in zip(names, raw):
        checks = [alignment(raw[0], x, t, c) for t in (10, 30, 50) for c in (0, 1)]
        lags = [p[0] for p in checks]
        corr = min(p[1] for p in checks)
        if max(lags) - min(lags) > 1 or corr < 0.999:
            raise ValueError(f"{name}: failed common-source alignment check: {checks}")
        lag = int(np.rint(np.median(lags)))
        samples = np.array(x[START + lag : START + lag + LENGTH])
        if samples.shape != (LENGTH, 2) or not np.isfinite(samples).all():
            raise ValueError(f"{name}: incomplete or non-finite excerpt")
        # The archived AIFF is PCM24. Round the other outputs to the same word
        # length, with zero added dither, before comparison-only gain matching.
        if np.max(np.abs(samples)) >= 1:
            raise ValueError("24-bit clipping in programme excerpt")
        if name != "Saracon":
            samples = np.round(samples * 2**23) / 2**23
        match = signal.sosfiltfilt(SOS, samples, axis=0)
        level = np.sqrt(np.mean(match[2048:-2048] ** 2))
        levels.append(level)
        gain = levels[0] / level
        f, psd = signal.welch(
            samples * gain,
            fs=FS,
            window="hann",
            nperseg=NFFT,
            noverlap=NFFT // 2,
            detrend=False,
            axis=0,
            scaling="density",
        )
        psds.append(np.mean(psd, axis=1))
        print(
            f"{name}: file offset {lag:+} frames, checks {lags}, minimum r {corr:.8f}, analysis gain {20 * np.log10(gain):+.6f} dB"
        )
    df = FS / NFFT
    psds = np.array(psds)
    with open("music-bands.csv", "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(("lower_hz", "upper_hz", *(n.lower() + "_dbfs" for n in names)))
        for lo, hi in BANDS:
            power = psds[:, (f >= lo) & (f < hi)].sum(axis=1) * df
            values = 10 * np.log10(power / 0.5)
            print(lo, hi, values)
            writer.writerow([lo, hi, *(f"{v:.5f}" for v in values)])
    # Equal-width (1 kHz) bins. Average power density before converting to dB.
    with open("music-spectrum.csv", "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["bin_center_hz", *(n.lower() + "_dbfs_per_hz" for n in names)])
        for lo in range(0, 88000, 1000):
            v = 10 * np.log10(
                psds[:, (f >= max(20, lo)) & (f < lo + 1000)].mean(axis=1) / 0.5
            )
            writer.writerow(
                [max(20, lo) / 2 + (lo + 1000) / 2, *(f"{x:.5f}" for x in v)]
            )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pcm", type=Path, nargs="+")
    main(cli.parse_args().pcm)
