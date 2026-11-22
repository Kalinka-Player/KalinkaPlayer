#!/usr/bin/env python
"""Build data/deam_manifest.csv from a DEAM download.

DEAM (MediaEval Database for Emotional Analysis in Music): 1,802 songs with
static per-song valence and arousal means on a 1-9 scale. Pass the static
annotation CSVs (static_annotations_averaged_songs_1_2000.csv and
static_annotations_averaged_songs_2000_2058.csv) and the MEMD_audio folder.
Rows are sorted by song id: the V/A split depends on this order.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kit_paths import DEAM_MANIFEST  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", nargs="+", required=True)
    ap.add_argument("--audio-dir", required=True)
    ap.add_argument("--out", default=str(DEAM_MANIFEST))
    args = ap.parse_args()
    labels = {}
    for path in args.annotations:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            keys = {k.strip(): k for k in reader.fieldnames or []}
            for row in reader:
                try:
                    sid = str(int(float(row[keys["song_id"]].strip())))
                    labels[sid] = (float(row[keys["valence_mean"]]), float(row[keys["arousal_mean"]]))
                except (KeyError, ValueError):
                    continue
    rows = [(sid, os.path.join(args.audio_dir, f"{sid}.mp3"), v, a)
            for sid, (v, a) in sorted(labels.items(), key=lambda kv: int(kv[0]))
            if os.path.isfile(os.path.join(args.audio_dir, f"{sid}.mp3"))]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["song_id", "audio_path", "valence_mean", "arousal_mean"])
        w.writerows(rows)
    print(f"{len(rows)} songs -> {args.out}")


if __name__ == "__main__":
    main()
