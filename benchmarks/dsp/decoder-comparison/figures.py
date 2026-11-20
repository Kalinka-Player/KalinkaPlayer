"""Plot article figures from the captures and band powers in a results directory."""

import argparse
import csv
import subprocess
from pathlib import Path
from xml.dom import minidom

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from measure import aligned_capture, frequency_response

BACKGROUND = "#111214"
INK = "#e3e5e6"
MUTED = "#b1b5b8"
GRID = "#35383b"
KALINKA_ACCENT = "#c2394b"  # --accent in the Kalinka website's site.css
KALINKA_PRESETS = {
    "30 kHz": "-",
    "50 kHz": (0, (1.3, 2.4)),
    "Wide": (0, (6, 2.8)),
}
FFMPEG = ("#ffffff", (0, (4.5, 2, 1.2, 2)))
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
    svg = out / (name + ".svg")
    fig.savefig(
        svg,
        metadata={"Title": title, "Date": None, "Creator": "Kalinka Player"},
    )
    # Matplotlib sets the SVG viewport in print points. Size the standalone
    # drawing in CSS pixels while preserving its viewBox and plot geometry.
    width = 1024 if narrow else 1800
    height = round(width * fig.get_figheight() / fig.get_figwidth())
    with minidom.parse(str(svg)) as document:
        document.documentElement.setAttribute("width", str(width))
        document.documentElement.setAttribute("height", str(height))
        svg.write_bytes(document.toxml(encoding="utf-8"))
    if not narrow:
        fig.savefig(
            out / (name + ".png"),
            dpi=width / fig.get_figwidth(),
            metadata={"Software": "Kalinka Player"},
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
        ["rsvg-convert", "--output", out / "stages.png", svg],
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
    for trace, (name, style) in zip(traces, KALINKA_PRESETS.items(), strict=True):
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
            color=KALINKA_ACCENT,
            ls=style,
            lw=0.945,
            label=name,
        )
        ax.plot(
            cutoff / 1000,
            -3,
            marker="o",
            ms=4.5,
            mec=KALINKA_ACCENT,
            mfc=BACKGROUND,
            mew=0.7,
        )
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
        color=KALINKA_ACCENT,
        lw=0.63,
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
    for trace, (name, style) in zip(traces, KALINKA_PRESETS.items(), strict=True):
        nonzero = np.flatnonzero(trace)
        t = (np.arange(len(trace)) - (nonzero[0] + nonzero[-1]) / 2) * 1e6 / BIT_RATE
        detail.plot(
            t, trace / trace.max(), color=KALINKA_ACCENT, ls=style, lw=0.91, label=name
        )
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
    for field, label, (color, style) in (
        ("kalinka", "Kalinka 30 kHz", (KALINKA_ACCENT, KALINKA_PRESETS["30 kHz"])),
        ("saracon", "Saracon", ("#a6e548", (0, (2.4, 2.4)))),
        ("ffmpeg", "FFmpeg", FFMPEG),
    ):
        key = field + "_dbfs_per_hz"
        if key in measurements.dtype.names:
            ax.plot(
                measurements["bin_center_hz"] / 1000,
                measurements[key],
                color=color,
                ls=style,
                lw=0.98,
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
    series = (
        ("kalinka-30k", "Kalinka 30 kHz", (KALINKA_ACCENT, KALINKA_PRESETS["30 kHz"])),
        ("kalinka-wide", "Kalinka Wide", (KALINKA_ACCENT, KALINKA_PRESETS["Wide"])),
        ("mpd", "MPD", ("#ffc34d", (0, (2.4, 2.4)))),
        ("ffmpeg", "FFmpeg", FFMPEG),
        ("squeezelite", "Squeezelite", ("#37d6ff", (0, (8, 2, 2, 2)))),
    )
    with (out / "comparison.csv").open() as stream:
        cutoffs = {
            row["converter"]: float(row["minus3db_hz"])
            for row in csv.DictReader(stream)
        }
    # Include 20 kHz exactly, then keep every measured bin through Nyquist.
    curves = {}
    for key, _, _ in series:
        path = out / (key + ".wav")
        f, db = frequency_response(path, *aligned_capture(path))
        band = (f > 20000) & (f <= PCM_RATE / 2)
        hz = np.r_[20000, f[band]]
        curves[key] = np.r_[np.interp(20000, f, db), db[band]]
    np.savetxt(
        out / "resampler-response.csv",
        np.column_stack((hz, *curves.values())),
        delimiter=",",
        fmt="%.8f",
        comments="",
        header="input_hz," + ",".join(key.replace("-", "_") + "_db" for key in curves),
    )
    for narrow in (False, True):
        fig = plt.figure(figsize=(4.05, 4.65) if narrow else (6.5, 3.0))
        heading(fig, "Ultrasonic response", "DSD64  /  176.4 kHz")
        if narrow:
            overview = graph(fig, [0.17, 0.50, 0.79, 0.21])
            detail = graph(fig, [0.17, 0.12, 0.79, 0.21])
        else:
            overview = graph(fig, [0.108, 0.19, 0.42, 0.48])
            detail = graph(fig, [0.64, 0.19, 0.325, 0.48])
        overview.set(
            xlim=(20, 88.2),
            ylim=(-100, 6),
            xticks=[20, 40, 60, 80],
            yticks=[0, -25, -50, -75, -100],
            ylabel="Gain relative to DC (dB)",
        )
        detail.set(
            xlim=(67, 88.2),
            ylim=(-12, 1),
            xticks=[70, 75, 80, 85],
            yticks=[0, -3, -6, -9, -12],
        )
        detail.axhline(-3, color=MUTED, linewidth=0.6, linestyle=":")
        for ax, title in [
            (overview, "Full ultrasonic band"),
            (detail, "Upper-band detail"),
        ]:
            ax.set_title(title, loc="left", fontsize=8.7, pad=5)
            for key, label, (color, style) in series:
                ax.plot(
                    hz / 1000, curves[key], label=label, color=color, ls=style, lw=0.91
                )
                ax.plot(
                    cutoffs[key] / 1000,
                    -3,
                    marker="o",
                    ms=4,
                    mec=color,
                    mfc=BACKGROUND,
                    mew=0.63,
                )
        fig.legend(
            *overview.get_legend_handles_labels(),
            loc="center left",
            bbox_to_anchor=(0.025, 0.834),
            ncol=3 if narrow else 5,
            fontsize=8.2,
            handlelength=1.65,
            handletextpad=0.42,
            columnspacing=0.9,
        )
        fig.text(0.54, 0.035, "Input frequency (kHz)", ha="center")
        save(
            fig,
            out,
            "resampler-response",
            "Measured ultrasonic response of Kalinka 30 kHz, Kalinka Wide, MPD, FFmpeg and Squeezelite",
            narrow,
        )


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("results", type=Path)
    cli.add_argument("--music", action="store_true")
    cli.add_argument(
        "--pi",
        action="store_true",
        help="Plot all five Pi conversion paths from the captured WAVs and comparison.csv",
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
