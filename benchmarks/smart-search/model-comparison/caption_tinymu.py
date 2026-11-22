#!/usr/bin/env python
"""Caption every production fragment window of a corpus with TinyMU.

Resumes from <cache>/<corpus>/captions_e87ad2bb.jsonl (the slug is the prompt's
SHA-1). Needs requirements-tinymu.txt in its own Python 3.11 environment.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tinymu_captioner as tm  # noqa: E402
from corpora import LOADERS  # noqa: E402
from kit_paths import CACHE, TINYMU  # noqa: E402

PROMPT = "Describe the music you hear."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora", default="mtg600,mtgshard,deam")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()
    model = None

    def get_model():
        nonlocal model
        if model is None:
            model = tm.TinyMU(TINYMU, TINYMU / "TinyMU", args.threads)
        return model

    for name in args.corpora.split(","):
        c = LOADERS[name]()
        out = CACHE / name
        out.mkdir(parents=True, exist_ok=True)
        caps, spw = tm.tinymu_captions(c, out, PROMPT, get_model, args.batch)
        print(f"[{name}] {len(caps)} window captions, {spw:.2f} s/window", flush=True)


if __name__ == "__main__":
    main()
