#!/usr/bin/env python
"""Time one model on this machine: analysing full tracks and encoding queries.

Run each model in its own process so peak memory is that model's alone:

  python benchmark_speed.py --model clap   --threads 4
  python benchmark_speed.py --model clap2s --threads 4
  .venv-tinymu/bin/python benchmark_speed.py --model tinymu --threads 4

A track is analysed the way the comparison does it: the production fragment rule
(three 10 s windows at 25/50/75 % for a full-length track), decoded, resampled
and encoded (CLAP, CLAP2-S) or captioned and embedded (TinyMU). The tracks are
the first full-length MTG-Jamendo tracks of the shard. Results are appended to
speed.csv: median seconds per track and per query, load time, peak RSS.
"""
from __future__ import annotations

import argparse
import csv
import os
import resource
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clap_embed as ce  # noqa: E402
from corpora import load_mtgshard  # noqa: E402
from kit_paths import MODELS, TINYMU  # noqa: E402

QUERIES = ["sad piano", "energetic workout music", "calm relaxing music to unwind", "jazz music",
           "music with violin", "dark and brooding", "happy acoustic song", "electronic music"]


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def setup_clap(threads):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads, opts.inter_op_num_threads = threads, 1
    audio = ort.InferenceSession(str(MODELS / "clap_audio_encoder.onnx"), opts, providers=["CPUExecutionProvider"])
    text = ort.InferenceSession(str(MODELS / "clap_text_encoder.onnx"), opts, providers=["CPUExecutionProvider"])
    tok = ce.make_tokenizer(str(MODELS / "clap_tokenizer.json"))
    return (lambda path: ce.embed_audio_file(audio, path)), (lambda q: ce.embed_text(text, tok, q))


def setup_clap2s(threads):
    import open_clip
    import torch
    from torch.nn.utils.rnn import pad_sequence
    from embed_clap2 import HUB, read_windows, starts_frag10
    torch.set_num_threads(threads)
    model, preprocess = open_clip.create_model_from_pretrained(HUB)
    model.eval()
    pp, tok = preprocess(), open_clip.get_tokenizer(HUB)

    def track(path):
        items = [pp(w) for w in read_windows(path, starts_frag10)]
        batch = {k: pad_sequence([x[k] for x in items], batch_first=True) for k in items[0]}
        with torch.no_grad():
            return model.encode_audio(batch, normalize=False).mean(0)

    def query(q):
        with torch.no_grad():
            return model.encode_text(tok([q]), normalize=True)
    return track, query


def setup_tinymu(threads):
    import tinymu_captioner as tm
    from minilm_onnx import MiniLmOnnx
    os.environ["MINILM_THREADS"] = str(threads)
    model = tm.TinyMU(TINYMU, TINYMU / "TinyMU", threads)
    minilm = MiniLmOnnx(str(MODELS / "minilm"))
    minilm.load()

    def track(path):
        waves = [tm.read_window_16k(path, s) for s in tm.windows(path)]
        groups: dict[int, list] = {}         # equal-length windows share a batch, as in captioning
        for w in waves:
            groups.setdefault(w.shape[0], []).append(w)
        caps = []
        for ws in groups.values():
            caps += model.caption(ws, "Describe the music you hear.")
        return minilm.encode(caps).mean(0)
    return track, (lambda q: minilm.encode([q]))


SETUP = {"clap": setup_clap, "clap2s": setup_clap2s, "tinymu": setup_tinymu}
LABEL = {"clap": "LAION-CLAP music (ONNX)", "clap2s": "CLAP2-S (PyTorch)", "tinymu": "TinyMU + MiniLM (PyTorch)"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(SETUP))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--tracks", type=int, default=0, help="default: 20, or 8 for TinyMU")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results" / "speed.csv"))
    args = ap.parse_args()
    n_tracks = args.tracks or (8 if args.model == "tinymu" else 20)
    c = load_mtgshard()
    paths = [p for p in c.paths if len(ce.fragment_starts_s(__import__("soundfile").info(p).duration)) == 3]

    t0 = time.perf_counter()
    track, query = SETUP[args.model](args.threads)
    load_s = time.perf_counter() - t0
    track(paths[0])                          # warm-up
    query(QUERIES[0])
    per_track = []
    for p in paths[1:n_tracks + 1]:
        t = time.perf_counter()
        track(p)
        per_track.append(time.perf_counter() - t)
    per_query = []
    for _ in range(3):
        for q in QUERIES:
            t = time.perf_counter()
            query(q)
            per_query.append(time.perf_counter() - t)
    row = {"model": LABEL[args.model], "threads": args.threads, "tracks_timed": len(per_track),
           "seconds_per_track_median": f"{statistics.median(per_track):.3f}",
           "seconds_per_track_min": f"{min(per_track):.3f}", "seconds_per_track_max": f"{max(per_track):.3f}",
           "ms_per_query_median": f"{statistics.median(per_query) * 1000:.1f}",
           "load_seconds": f"{load_s:.1f}", "peak_rss_mb": f"{peak_rss_mb():.0f}",
           "cpu": open("/proc/cpuinfo").read().split("model name")[1].split(":")[1].split("\n")[0].strip()
           if os.path.exists("/proc/cpuinfo") else ""}
    print(row)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    with open(out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


if __name__ == "__main__":
    main()
