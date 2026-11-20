"""Measure a mono 176.4 kHz float64 WAV rendered from dsd64.dsf.

Usage: python measure.py OUTPUT.wav [ANOTHER.wav ...]
Requires NumPy and SciPy. Read README.txt for the fixture and output settings.
"""

import argparse
from pathlib import Path

import numpy as np
from scipy.io import wavfile

PCM = 176400
RATIO = 16
SLOT = 2048
BIT_AT = 768
TONE_START = RATIO * SLOT
NFFT = 262144


def aligned_capture(path):
    rate, output = wavfile.read(path)
    if rate != PCM or output.ndim != 1 or output.dtype != np.float64:
        raise ValueError(f"{path}: export a mono, 176400 Hz, float64 WAV")
    if len(output) < TONE_START + PCM // 2 or not np.isfinite(output).all():
        raise ValueError(f"{path}: incomplete or non-finite recording")

    # Use a common PCM-frame shift for all 16 decimation phases.
    first = output[:SLOT]
    lag = int(np.argmax(abs(first - np.median(first)))) - BIT_AT
    if np.ptp(first) < 0.001 or abs(lag) >= SLOT // 4:
        raise ValueError(f"{path}: alignment marker missing or outside supported range")
    return output, lag


def frequency_response(path, output, lag):
    impulse = np.empty(SLOT * RATIO)
    for phase in range(RATIO):
        start = phase * SLOT + BIT_AT + lag - SLOT // 2
        # The first slot begins in digital silence, including before sample 0.
        indices = np.arange(start, start + SLOT)
        block = np.zeros(SLOT)
        mask = indices >= 0
        block[mask] = output[indices[mask]]
        # Exclude DC in the idle output. The edges should be clear of the lobe.
        block -= np.median(np.r_[block[:32], block[-32:]])
        if max(abs(block[:32]).max(), abs(block[-32:]).max()) > 1e-6 * max(abs(block)):
            raise ValueError(f"{path}: impulse exceeds the capture interval")
        change = -2 if (0x69 >> (7 - phase % 8)) & 1 else 2
        impulse[RATIO - phase - 1 :: RATIO] = block / change
    f = np.fft.rfftfreq(NFFT, 1 / (PCM * RATIO))
    gain = abs(np.fft.rfft(impulse, NFFT))
    response = 20 * np.log10(np.maximum(gain / gain[0], 1e-300))
    return f, response


def measure(path):
    output, lag = aligned_capture(path)
    f, response = frequency_response(path, output, lag)
    hits = np.flatnonzero((f <= PCM / 2) & (response <= -3))
    crossing = np.nan
    if len(hits) and hits[0] > 0:
        k = hits[0]
        crossing = np.interp(-3, response[k - 1 : k + 1][::-1], f[k - 1 : k + 1][::-1])

    # A fixed 0.25 s excerpt in the middle of the 0.50 s tone, aligned by the marker.
    start = TONE_START + PCM // 8 + lag
    segment = output[start : start + PCM // 4]
    n = np.arange(len(segment))
    # Periodic seven-term Blackman–Harris, with the release harness coefficients.
    coefficients = [
        0.27105140069342,
        0.43329793923448,
        0.21812299954311,
        0.06592544638803,
        0.01081174209837,
        0.00077658482522,
        0.00001388721735,
    ]
    win = sum(
        (-1) ** k * c * np.cos(2 * np.pi * k * n / len(n))
        for k, c in enumerate(coefficients)
    )
    phase = 2 * np.pi * (1000 / PCM) * n
    basis = np.column_stack((np.cos(phase), np.sin(phase), np.ones_like(phase)))
    fit = np.linalg.lstsq(basis * win[:, None], segment * win, rcond=None)[0]
    amplitude = np.hypot(fit[0], fit[1])
    if abs(20 * np.log10(amplitude) - (-6.0206)) > 0.02:
        raise ValueError(
            f"{path}: check output gain, channel routing and file identity"
        )
    error = (segment - basis @ fit) * win
    spectrum = np.fft.rfft(error)
    bins = np.fft.rfftfreq(len(error), 1 / PCM)
    power = (
        2
        * np.sum(abs(spectrum[(bins >= 20) & (bins <= 20000)]) ** 2)
        / (len(error) * np.sum(win**2))
    )
    return crossing, 20 * np.log10(amplitude), 10 * np.log10(power / (amplitude**2 / 2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, nargs="+")
    args = parser.parse_args()
    print("file,minus3db_hz,tone_dbfs,thdn_db")
    for path in args.wav:
        cutoff, tone, noise = measure(path)
        print(f"{path.name},{cutoff:.1f},{tone:.5f},{noise:.3f}")
