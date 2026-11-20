"""Plot article figures from the captures and band powers in a results directory."""

import argparse
import csv
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from measure import aligned_capture, frequency_response

BACKGROUND = "#111214"
INK = "#e3e5e6"
MUTED = "#b1b5b8"
GRID = "#35383b"
COLORS = ("#efa7ab", "#9ed3ce", "#d2bd8e")
LINES = ("-", (0, (5, 2.4)), (0, (1.3, 1.7)))
NAMES = ("30 kHz", "50 kHz", "Wide")
BIT_RATE = 2822400
PCM_RATE = 176400
FFT_SIZE = 262144

plt.rcParams.update(
    {
        "figure.facecolor": BACKGROUND,
        "axes.facecolor": BACKGROUND,
        "text.color": INK,
        "axes.labelcolor": INK,
        "axes.edgecolor": "#62676b",
        "axes.linewidth": 0.55,
        "axes.axisbelow": True,
        "axes.labelpad": 4,
        "font.family": "DejaVu Sans",
        "font.size": 8.8,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "xtick.major.width": 0.55,
        "ytick.major.width": 0.55,
        "xtick.major.pad": 3.5,
        "ytick.major.pad": 3.5,
        "legend.frameon": False,
        "svg.hashsalt": "kalinka-decoder-520",
        "svg.fonttype": "path",
    }
)


def graph(fig, bounds):
    ax = fig.add_axes(bounds)
    ax.grid(color=GRID, linewidth=0.45)
    return ax


def heading(fig, label, spec):
    fig.text(0.04, 0.938, label, color=INK, weight="bold", fontsize=10, va="center")
    fig.text(0.96, 0.938, spec, color=MUTED, fontsize=8.2, va="center", ha="right")


def legend(fig, ax, y):
    fig.legend(
        *ax.get_legend_handles_labels(),
        loc="center left",
        bbox_to_anchor=(0.024, y),
        ncol=3,
        fontsize=8.4,
        handlelength=2.1,
        handletextpad=0.45,
        columnspacing=1.2,
    )


def save(fig, out, name, title, narrow):
    if narrow:
        name += "-mobile"
    fig.savefig(
        out / (name + ".svg"),
        metadata={"Title": title, "Date": None, "Creator": "Kalinka Player"},
    )
    if not narrow:
        fig.savefig(
            out / (name + ".png"), dpi=210, metadata={"Software": "Kalinka Player"}
        )
    plt.close(fig)


def pipeline(out):
    with (out / "chains.csv").open() as stream:
        chain = next(row for row in csv.DictReader(stream) if row["preset"] == "30 kHz")
    template = Path(__file__).with_name("stages.svg.in").read_text()
    counts = {
        name: int(chain[name])
        for name in ("stage1_taps", "halfband_taps", "output_taps")
    }
    svg = out / "stages.svg"
    svg.write_text(template.format_map(counts))
    subprocess.run(
        ["rsvg-convert", "--zoom", "3", "--output", out / "stages.png", svg],
        check=True,
    )


def frequency_plot(out, traces, narrow):
    fig = plt.figure(figsize=(4.05 if narrow else 6.1, 2.72))
    heading(fig, "Frequency response", "DSD64  /  176.4 kHz")
    left = 0.158 if narrow else 0.11
    ax = graph(fig, [left, 0.205, 0.965 - left, 0.51])
    freq = np.fft.rfftfreq(FFT_SIZE, 1 / BIT_RATE)
    baseband = freq <= PCM_RATE / 2
    ax.axvspan(0, 20, color="#7d9599", alpha=0.09, lw=0)
    response_rows = []
    for trace, name, color, style in zip(traces, NAMES, COLORS, LINES):
        spectrum = np.abs(np.fft.rfft(trace, FFT_SIZE))
        absolute = 20 * np.log10(np.maximum(spectrum, 1e-25))
        db = absolute - absolute[0]
        k = np.flatnonzero(db <= -3)[0]
        cutoff = np.interp(-3, db[k - 1 : k + 1][::-1], freq[k - 1 : k + 1][::-1])
        folded = freq % PCM_RATE
        aliases = (freq > PCM_RATE / 2) & (
            (folded <= 20000) | (folded >= PCM_RATE - 20000)
        )
        response_rows.append(
            (name, cutoff, max(abs(absolute[freq <= 20000])), max(absolute[aliases]))
        )
        ax.plot(
            freq[baseband] / 1000,
            db[baseband],
            color=color,
            ls=style,
            lw=1.35,
            label=name,
        )
        ax.plot(cutoff / 1000, -3, marker="o", ms=4.5, mec=color, mfc=BACKGROUND, mew=1)
    ax.set(
        xlim=(0, 88.2),
        ylim=(-122, 8),
        xticks=[0, 20, 40, 60, 80],
        yticks=[-120, -90, -60, -30, 0],
        xlabel="Input frequency (kHz)",
        ylabel="Gain (dB)",
    )
    legend(fig, ax, 0.811)
    save(
        fig,
        out,
        "frequency-response",
        "Kalinka filter frequency response; circles indicate −3 dB",
        narrow,
    )
    if not narrow:
        with (out / "filter-results.csv").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "preset",
                    "minus3db_hz",
                    "max_abs_audio_deviation_db",
                    "worst_audio_alias_db",
                ]
            )
            writer.writerows(response_rows)


def impulse_plot(out, traces, narrow):
    fig = plt.figure(figsize=(4.05, 4.02) if narrow else (6.1, 2.95))
    heading(fig, "Impulse response", "DSD64  /  176.4 kHz")
    if narrow:
        top = graph(fig, [0.17, 0.575, 0.79, 0.225])
        detail = graph(fig, [0.17, 0.152, 0.79, 0.225])
    else:
        top = graph(fig, [0.108, 0.236, 0.356, 0.495])
        detail = graph(fig, [0.606, 0.236, 0.356, 0.495])
    top.set_title("PCM samples · 30 kHz", loc="left", fontsize=8.7, pad=5.5)
    samples = traces[0, 15::16]
    top.plot(
        np.arange(len(samples)) * 1000 / PCM_RATE,
        samples,
        color=COLORS[0],
        lw=0.9,
        marker=".",
        ms=3.2,
    )
    top.set(
        xlim=(0, 0.43),
        ylim=(-0.006, 0.023),
        xticks=[0, 0.1, 0.2, 0.3, 0.4],
        yticks=[0, 0.01, 0.02],
        xlabel="Time after bit change (ms)",
        ylabel="Amplitude",
    )
    detail.set_title("Peak-normalised filters", loc="left", fontsize=8.7, pad=5.5)
    for trace, name, color, style in zip(traces, NAMES, COLORS, LINES):
        nonzero = np.flatnonzero(trace)
        t = (np.arange(len(trace)) - (nonzero[0] + nonzero[-1]) / 2) * 1e6 / BIT_RATE
        detail.plot(t, trace / trace.max(), color=color, ls=style, lw=1.3, label=name)
    detail.set(
        xlim=(-45, 45),
        ylim=(-0.28, 1.1),
        xticks=[-40, -20, 0, 20, 40],
        yticks=[0, 0.5, 1],
        xlabel="Time from centre (µs)",
        ylabel="Relative peak",
    )
    legend(fig, detail, 0.039 if narrow else 0.048)
    save(
        fig,
        out,
        "impulse-response",
        "Causal PCM impulse and aligned Kalinka filter shapes",
        narrow,
    )


def music_plot(out, narrow):
    fig = plt.figure(figsize=(4.05 if narrow else 6.1, 2.82))
    heading(fig, "Output power density", "30–40 s  /  stereo")
    left = 0.178 if narrow else 0.13
    ax = graph(fig, [left, 0.205, 0.965 - left, 0.52])
    ax.axvspan(0, 20, color="#7d9599", alpha=0.09, lw=0)
    measurements = np.genfromtxt(out / "music-spectrum.csv", delimiter=",", names=True)
    for field, label, color, style in zip(
        ("kalinka", "saracon", "ffmpeg"),
        ("Kalinka 30 kHz", "Saracon", "FFmpeg"),
        COLORS,
        LINES,
    ):
        key = field + "_dbfs_per_hz"
        if key in measurements.dtype.names:
            ax.plot(
                measurements["bin_center_hz"] / 1000,
                measurements[key],
                color=color,
                ls=style,
                lw=1.4,
                label=label,
            )
    ax.set(
        xlim=(0, 88.2),
        ylim=(-205, -48),
        xticks=[0, 20, 40, 60, 80],
        yticks=[-200, -150, -100, -50],
        xlabel="Frequency (kHz)",
        ylabel="Density (dBFS/Hz)",
    )
    legend(fig, ax, 0.818)
    save(
        fig,
        out,
        "music-spectrum",
        "Matched stereo spectra of Looking for a Home, 30 to 40 seconds",
        narrow,
    )


def pi_response(out):
    names = ("MPD", "FFmpeg", "Squeezelite")
    curves = []
    for name in names:
        path = out / (name.lower() + ".wav")
        f, db = frequency_response(path, *aligned_capture(path))
        # Keep one FFT bin in eight (~86 Hz spacing) in the plotted range.
        bins = np.flatnonzero((f >= 58000) & (f <= PCM_RATE / 2))[::8]
        curves.append(db[bins])
    hz = f[bins]
    np.savetxt(
        out / "resampler-response.csv",
        np.column_stack((hz, *curves)),
        delimiter=",",
        fmt="%.8f",
        comments="",
        header="input_hz,mpd_db,ffmpeg_db,squeezelite_db",
    )
    with (out / "comparison.csv").open() as stream:
        cutoffs = {
            row["converter"]: float(row["minus3db_hz"])
            for row in csv.DictReader(stream)
        }
    for narrow in (False, True):
        fig = plt.figure(figsize=(4.05 if narrow else 6.1, 2.72))
        heading(fig, "Ultrasonic response", "DSD64  /  176.4 kHz")
        left = 0.157 if narrow else 0.11
        ax = graph(fig, [left, 0.205, 0.965 - left, 0.51])
        ax.axhline(-3, color=MUTED, linewidth=0.6, linestyle=":")
        for name, db, color, style in zip(names, curves, COLORS, LINES):
            ax.plot(hz / 1000, db, label=name, color=color, ls=style, lw=1.45)
            ax.plot(
                cutoffs[name.lower()] / 1000,
                -3,
                marker="o",
                ms=4.6,
                mec=color,
                mfc=BACKGROUND,
                mew=1,
            )
        ax.set(
            xlim=(58, 88.2),
            ylim=(-18, 1),
            xticks=[60, 65, 70, 75, 80, 85],
            yticks=[0, -3, -6, -9, -12, -15, -18],
            xlabel="Input frequency (kHz)",
            ylabel="Gain relative to DC (dB)",
        )
        legend(fig, ax, 0.81)
        save(
            fig,
            out,
            "resampler-response",
            "Measured ultrasonic response of the MPD, FFmpeg and Squeezelite conversion paths",
            narrow,
        )


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("results", type=Path)
    cli.add_argument("--music", action="store_true")
    cli.add_argument(
        "--pi",
        action="store_true",
        help="Plot the Pi resampler comparison from three WAVs and comparison.csv",
    )
    args = cli.parse_args()
    if args.pi:
        pi_response(args.results)
        return
    traces = np.loadtxt(args.results / "impulse.csv", delimiter=",", skiprows=1)[
        :, 1:
    ].T
    pipeline(args.results)
    for narrow in (False, True):
        frequency_plot(args.results, traces, narrow)
        impulse_plot(args.results, traces, narrow)
        if args.music:
            music_plot(args.results, narrow)


if __name__ == "__main__":
    main()
