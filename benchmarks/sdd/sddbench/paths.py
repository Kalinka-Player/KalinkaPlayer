"""Where the benchmark keeps its inputs and its outputs.

Two roots, because they have different lifetimes: the dataset and the model
cache are expensive to fetch and shared by every run, while a run's artifacts
belong to that run alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_ROOT = Path(
    os.environ.get("KALINKA_SDD_BENCH_CACHE", "~/.cache/kalinka-sdd-bench")
).expanduser()


@dataclass(frozen=True)
class Layout:
    """Every path the benchmark reads or writes."""

    out: Path

    @property
    def dataset(self) -> Path:
        return CACHE_ROOT / "dataset"

    @property
    def audio(self) -> Path:
        """The indexed library: opaque filenames, tags stripped."""
        return CACHE_ROOT / "audio"

    @property
    def models(self) -> Path:
        """CLAP checkpoints, kept across runs (the server dir symlinks here)."""
        return CACHE_ROOT / "models"

    @property
    def judge_model(self) -> Path:
        return CACHE_ROOT / "judge"

    @property
    def captions_csv(self) -> Path:
        return self.dataset / "song_describer.csv"

    @property
    def audio_zip(self) -> Path:
        return self.dataset / "audio.zip"

    @property
    def manifest(self) -> Path:
        return self.out / "manifest.csv"

    @property
    def dataset_stats(self) -> Path:
        return self.out / "dataset_stats.json"

    @property
    def index_timings(self) -> Path:
        return self.out / "index_timings.csv"

    @property
    def index_summary(self) -> Path:
        return self.out / "index_summary.json"

    @property
    def caption_similarity(self) -> Path:
        return self.out / "caption_similarity.csv"

    @property
    def fingerprint(self) -> Path:
        return self.out / "fingerprint.json"

    @property
    def timeline(self) -> Path:
        return self.out / "timeline.csv"

    @property
    def report(self) -> Path:
        return self.out / "results.md"

    def results(self, run: str) -> Path:
        return self.out / f"results_{run}.csv"

    def metrics(self, run: str) -> Path:
        return self.out / f"metrics_{run}.json"

    def server_log(self, run: str) -> Path:
        return self.out / f"server_{run}.log"

    def ensure(self) -> "Layout":
        for path in (self.out, self.dataset, self.audio.parent, self.models):
            path.mkdir(parents=True, exist_ok=True)
        return self
