#!/usr/bin/env python
"""Embed the corpora with Kalinka's production CLAP audio encoder (clap-onnx-v2).

Production preprocessing from clap_embed.py: 48 kHz mono, int16 round trip,
1-3 fragments of 10 s at 25/50/75 % (33/66 % for 15-30 s, one for shorter),
mean-pooled and L2-normalised. Writes <cache>/<corpus>/clap_audio.npz (X, ids).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clap_embed as ce  # noqa: E402
from corpora import LOADERS  # noqa: E402
from kit_paths import CACHE, MODELS  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora", default="mtg600,mtgshard,deam")
    args = ap.parse_args()
    sess = ce.make_session(str(MODELS / "clap_audio_encoder.onnx"))
    for name in args.corpora.split(","):
        c = LOADERS[name]()
        out = CACHE / name
        out.mkdir(parents=True, exist_ok=True)
        X = np.zeros((len(c.ids), ce.EMBED_DIM), np.float32)
        for i, p in enumerate(c.paths):
            v = ce.embed_audio_file(sess, p)
            if v is not None:
                X[i] = v
            if (i + 1) % 100 == 0:
                print(f"  [{name}] {i + 1}/{len(c.ids)}", flush=True)
        np.savez(out / "clap_audio.npz", X=X, ids=np.array(c.ids))
        print(f"[{name}] {len(c.ids)} tracks -> {out / 'clap_audio.npz'}", flush=True)


if __name__ == "__main__":
    main()
