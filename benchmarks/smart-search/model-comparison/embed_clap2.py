#!/usr/bin/env python
"""Embed the eval corpora with CLAP2 (default S: laion/clap2-small-audioxl-s1B;
set CLAP2_SIZE=medium|large|xs|xxs for another size).

Two audio variants:
  frag10  the production fragment rule from clap_embed.py: 1-3 windows of 10 s
          at the same positions CLAP sees, mean-pooled. Same audio as CLAP.
          No int16 round trip or repeat-pad (those are CLAP preprocessing);
          CLAP2 takes native length and resamples to 32 kHz itself.
  seg30   CLAP2's native context: 30 s windows. <60 s -> one centred window,
          60-90 s -> 33/66 %, >=90 s -> 25/50/75 %; mean-pooled.

Per-window raw tower outputs are averaged, then L2-normalised (as production
does for CLAP). Caches: out/<corpus>/clap2s_<variant>.npz with X, ids,
sec_per_window, n_windows.

Needs requirements.txt (PyTorch CPU + open_clip_torch 4.2.2).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clap_embed as ce  # noqa: E402
from corpora import LOADERS  # noqa: E402
from kit_paths import CACHE  # noqa: E402

import os
SIZE = os.environ.get("CLAP2_SIZE", "small")          # xxs | xs | small | medium | large
HUB = f"hf-hub:laion/clap2-{SIZE}-audioxl-s1B"
TAG = {"xxs": "clap2xxs", "xs": "clap2xs", "small": "clap2s", "medium": "clap2m", "large": "clap2l"}[SIZE]


def starts_frag10(dur: float) -> tuple[list[float], float]:
    return ce.fragment_starts_s(dur), float(ce.FRAGMENT_SECONDS)


def starts_seg30(dur: float) -> tuple[list[float], float]:
    if dur < 60.0:
        return [max(0.0, (dur - 30.0) / 2)], 30.0
    ratios = (0.33, 0.66) if dur < 90.0 else (0.25, 0.50, 0.75)
    return [min(dur * r, dur - 30.0) for r in ratios], 30.0


VARIANTS = {"frag10": starts_frag10, "seg30": starts_seg30}


def read_windows(path: str, rule):
    import soundfile as sf
    import torch
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        dur = f.frames / float(sr)
        starts, secs = rule(dur)
        out = []
        for s in starts:
            f.seek(int(s * sr))
            x = f.read(int(secs * sr), dtype="float32", always_2d=False)
            if x.size:
                out.append((torch.from_numpy(np.ascontiguousarray(ce.to_mono(x)))[None], sr))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora", default="mtg600,mtgshard,deam")
    ap.add_argument("--variants", default="frag10,seg30")
    ap.add_argument("--out", default=str(CACHE))
    ap.add_argument("--threads", type=int, default=16)
    args = ap.parse_args()

    import open_clip
    import torch
    from torch.nn.utils.rnn import pad_sequence
    torch.set_num_threads(args.threads)
    model, preprocess = open_clip.create_model_from_pretrained(HUB)
    model.eval()
    pp = preprocess()

    for variant in args.variants.split(","):
        rule = VARIANTS[variant]
        for name in args.corpora.split(","):
            c = LOADERS[name]()
            out = Path(args.out) / name
            out.mkdir(parents=True, exist_ok=True)
            cache = out / f"{TAG}_{variant}.npz"
            if cache.exists() and list(np.load(cache)["ids"]) == c.ids:
                print(f"[{name}/{variant}] cached", flush=True)
                continue
            X = None
            n_win, t_enc, t0 = 0, 0.0, time.time()
            for i, p in enumerate(c.paths):
                wins = read_windows(p, rule)
                if not wins:
                    print(f"  no audio: {p}", flush=True)
                    continue
                items = [pp(w) for w in wins]
                batch = {k: pad_sequence([x[k] for x in items], batch_first=True) for k in items[0]}
                te = time.time()
                with torch.no_grad():
                    E = model.encode_audio(batch, normalize=False).float().numpy()
                t_enc += time.time() - te
                if X is None:
                    X = np.zeros((len(c.ids), E.shape[1]), np.float32)
                X[i] = ce.l2_normalize(E.mean(0))
                n_win += len(wins)
                if (i + 1) % 100 == 0:
                    print(f"  [{name}/{variant}] {i + 1}/{len(c.ids)}  "
                          f"{t_enc / n_win * 1000:.0f} ms/window encode, "
                          f"{(time.time() - t0) / 60:.1f} min", flush=True)
            np.savez(cache, X=X, ids=np.array(c.ids), sec_per_window=t_enc / max(1, n_win),
                     n_windows=n_win)
            print(f"[{name}/{variant}] done: {len(c.ids)} tracks, {n_win} windows, "
                  f"{t_enc / max(1, n_win) * 1000:.0f} ms/window -> {cache}", flush=True)


if __name__ == "__main__":
    main()
