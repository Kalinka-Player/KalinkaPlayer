#!/usr/bin/env python
"""Article figures from evaluate.py's summary.csv, in the kalinka-site blog style.

  matches.svg, matches-mobile.svg, matches.png
      matching tracks in the first ten results, by kind of search, for each
      model with its mood model, against a random pick
  model-comparison.png
      1200 x 630 social card
  matches.csv
      the plotted values

Dark background, DejaVu Sans, 1800 px desktop SVG and PNG, 1024 px mobile SVG,
as in benchmarks/dsp/decoder-comparison/figures.py.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from xml.dom import minidom

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BACKGROUND = "#111214"
INK = "#e3e5e6"
MUTED = "#b1b5b8"
GRID = "#35383b"
PINK = "#f49aa2"
TEAL = "#99dccc"

plt.rcParams.update({
    "figure.facecolor": BACKGROUND, "axes.facecolor": BACKGROUND, "text.color": INK,
    "axes.labelcolor": INK, "axes.edgecolor": "#62676b", "axes.linewidth": 0.55,
    "axes.axisbelow": True, "axes.labelpad": 4, "font.family": "DejaVu Sans", "font.size": 8.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.major.size": 2.5, "ytick.major.size": 0,
    "xtick.major.width": 0.55, "xtick.major.pad": 3.5, "ytick.major.pad": 6,
    "legend.frameon": False, "svg.hashsalt": "kalinka-model-comparison", "svg.fonttype": "path",
})

# (system in summary.csv, legend label, marker style)
SERIES = [
    ("CLAP + V/A", "LAION-CLAP + mood", dict(marker="o", markersize=6.2, markerfacecolor=BACKGROUND,
                                             markeredgecolor=INK, markeredgewidth=1.2)),
    ("CLAP2-S + V/A", "CLAP2-S + mood", dict(marker="o", markersize=5.4, color=PINK, markeredgewidth=0)),
    ("TinyMU + V/A", "TinyMU + mood", dict(marker="D", markersize=4.6, color=TEAL, markeredgewidth=0)),
]
# (label, [(corpus, category)], searches)
ROWS = [
    ("Genre", [("mtg600", "genre"), ("mtgshard", "genre")], 50),
    ("Instrument", [("mtg600", "instrument"), ("mtgshard", "instrument")], 30),
    ("Mood or theme tag", [("mtg600", "moodtheme"), ("mtgshard", "moodtheme")], 30),
    ("Everyday phrase", [("mtg600", "natural"), ("mtgshard", "natural")], 40),
    ("Mood, rated songs", [("deam_cv", "ALL")], 15),
]


def save(fig, out, name, title, narrow):
    if narrow:
        name += "-mobile"
    svg = out / (name + ".svg")
    fig.savefig(svg, metadata={"Title": title, "Date": None, "Creator": "Kalinka Player"})
    width = 1024 if narrow else 1800
    height = round(width * fig.get_figheight() / fig.get_figwidth())
    with minidom.parse(str(svg)) as document:
        document.documentElement.setAttribute("width", str(width))
        document.documentElement.setAttribute("height", str(height))
        svg.write_bytes(document.toxml(encoding="utf-8"))
    if not narrow:
        fig.savefig(out / (name + ".png"), dpi=width / fig.get_figwidth(), metadata={"Software": "Kalinka Player"})
    plt.close(fig)
    return width, height


def row_values(summary):
    s = {(r["corpus"], r["system"], r["category"]): r for r in summary}
    vals = {}
    for label, cells, _ in ROWS:
        for system, _, _ in SERIES:
            vals[(label, system)] = 10 * sum(float(s[(c, system, k)]["p_at_10"]) for c, k in cells) / len(cells)
        first = SERIES[0][0]
        vals[(label, "random")] = 10 * sum(float(s[(c, first, k)]["p_at_10"]) - float(s[(c, first, k)]["lift_at_10"])
                                           for c, k in cells) / len(cells)
    return vals


def matches_plot(out, summary, narrow):
    vals = row_values(summary)
    fig = plt.figure(figsize=(4.05 if narrow else 6.1, 3.5 if narrow else 3.05))
    fig.text(0.04, 0.95, "Matching tracks in the first ten results", color=INK, weight="bold", fontsize=10, va="center")
    left = 0.45 if narrow else 0.31
    ax = fig.add_axes([left, 0.15, 0.955 - left, 0.62 if narrow else 0.6])
    ax.grid(axis="x", color=GRID, linewidth=0.45)
    n = len(ROWS)
    for i, (label, _, _) in enumerate(ROWS):
        y = n - 1 - i
        r = vals[(label, "random")]
        ax.plot([r, r], [y - 0.22, y + 0.22], color=MUTED, linewidth=1.1, solid_capstyle="butt")
        xs = [vals[(label, s)] for s, _, _ in SERIES]
        ax.plot([min(xs), max(xs)], [y, y], color="#5b5f63", linewidth=1.0)
        for (system, _, style), x in zip(SERIES, xs):
            ax.plot([x], [y], linestyle="none", **style)
    ax.set_yticks(range(n)[::-1], [f"{label}  ·  {k}" for label, _, k in ROWS])
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_xlim(0, 10)
    ax.set_xticks(range(0, 11, 2))
    for side in ("left", "right", "top"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("Matching tracks out of 10" if narrow else "Matching tracks out of 10  ·  number: searches",
                  color=MUTED, fontsize=8.2)
    handles = [ax.plot([], [], linestyle="none", label=lab, **style)[0] for _, lab, style in SERIES]
    handles.append(ax.plot([], [], color=MUTED, linewidth=1.1, marker="|", markersize=7, linestyle="none",
                           label="Random pick")[0])
    fig.legend(handles=handles, loc="center left", bbox_to_anchor=(0.03, 0.86), ncol=2 if narrow else 4,
               fontsize=8.0, handletextpad=0.3, columnspacing=1.2)
    return save(fig, out, "matches", "Matching tracks in the first ten results, by kind of search", narrow)


def pooled_p10(summary, system):
    """Matching tracks out of 10 over all 165 searches (both MTG corpora + DEAM, cross-validated)."""
    rows = [r for r in summary if r["system"] == system and r["category"] == "ALL"
            and r["corpus"] in ("mtg600", "mtgshard", "deam_cv")]
    return 10 * sum(float(r["p_at_10"]) * int(r["queries"]) for r in rows) / sum(int(r["queries"]) for r in rows)


def social_card(out, summary):
    fig = plt.figure(figsize=(12, 6.3), dpi=100)
    fig.text(0.065, 0.86, "KALINKA PLAYER   /   MODEL COMPARISON", color=TEAL, family="DejaVu Sans Mono", fontsize=17,
             va="center")
    fig.text(0.065, 0.71, "Mood-search models", color="#f3efea", weight="bold", fontsize=54, va="center")
    fig.text(0.066, 0.575, "LAION-CLAP, CLAP2-S and TinyMU on 165 searches", color="#c2bcbb", fontsize=24, va="center")
    ax = fig.add_axes([0.32, 0.1, 0.6, 0.34])
    ys = range(len(SERIES))[::-1]
    for y, (system, lab, style) in zip(ys, SERIES):
        v = pooled_p10(summary, system)
        ax.plot([0, v], [y, y], color="#3a3d40", linewidth=7, solid_capstyle="butt")
        big = dict(style)
        big["markersize"] = style["markersize"] * 2.2
        if "markeredgewidth" in big and big["markeredgewidth"]:
            big["markeredgewidth"] = 2.4
        ax.plot([v], [y], linestyle="none", **big)
        fig.text(0.3, 0.1 + 0.34 * (y + 0.5) / len(SERIES), lab, color=INK, fontsize=19, ha="right", va="center")
        ax.text(v + 0.12, y, f"{v:.1f}", color=MUTED, fontsize=17, va="center")
    ax.set_xlim(0, 4.5)
    ax.set_ylim(-0.5, len(SERIES) - 0.5)
    ax.axis("off")
    fig.text(0.32, 0.06, "matching tracks in the first ten  ·  165 searches", color=MUTED, fontsize=14)
    fig.savefig(out / "model-comparison.png", dpi=100, metadata={"Software": "Kalinka Player"})
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="evaluate.py output directory")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(Path(args.results) / "summary.csv") as f:
        summary = list(csv.DictReader(f))
    for narrow in (False, True):
        print("matches", matches_plot(out, summary, narrow))
    social_card(out, summary)
    vals = row_values(summary)
    with open(out / "matches.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["search", "searches"] + [lab for _, lab, _ in SERIES] + ["random pick"])
        for label, _, k in ROWS:
            w.writerow([label, k] + [f"{vals[(label, sys_)]:.2f}" for sys_, _, _ in SERIES]
                       + [f"{vals[(label, 'random')]:.2f}"])
        w.writerow(["All searches", 165] + [f"{pooled_p10(summary, sys_):.2f}" for sys_, _, _ in SERIES] + [""])


if __name__ == "__main__":
    main()
