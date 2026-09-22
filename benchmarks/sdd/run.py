#!/usr/bin/env python3
"""Kalinka semantic-search benchmark on the Song Describer Dataset.

One command, five stages, two outputs: what the search is worth, and where
the time went. See RUNBOOK.md.

    python benchmarks/sdd/run.py --out tmp/sdd_bench/run1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sddbench import (
    dataset, fingerprint, indexing, judge, metrics, mood, report, retrieval,
)
from sddbench.clock import Clock
from sddbench.instance import MoodAblation, build
from sddbench.paths import Layout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sdd-bench").info

#: Each ranking configuration, and the valence/arousal setting it is named
#: for. The runner applies it per run, so a run measures what it says
#: whichever order the runs are asked in.
RUNS = {"mood_on": True, "mood_off": False}


def prepare(layout: Layout, clock: Clock, force: bool) -> dict:
    with clock.span("dataset_prep"):
        stats = dataset.prepare(layout, log=log, force=force)
    return stats


def index_library(layout: Layout, clock: Clock, top_k: int):
    instance = build(layout, mood=True, top_k=top_k)
    with clock.span("indexing_end_to_end"):
        summary = indexing.index(instance, layout, mood=True, log=log)
    log(f"indexing outcome: {json.dumps(summary['outcome'])}")
    (layout.out / "server_config.json").write_text(
        json.dumps(instance.get("/server/config"), indent=2)
    )
    return instance, summary


def retrieve(instance, layout: Layout, clock: Clock, queries, tracks, run_name: str,
             depth: int):
    queries = list(queries)
    measured = retrieval.measure(
        instance, clock, queries, tracks, layout, run_name, log=log
    )
    coverage = retrieval.attach_scores(
        measured.answers, queries, measured.log_slice, layout, tracks
    )
    retrieval.write_results(
        measured.answers, measured.timings, layout, run_name, depth
    )
    (layout.out / f"coverage_{run_name}.json").write_text(json.dumps(coverage, indent=2))
    log(f"{run_name}: {json.dumps(coverage)}")
    log(f"{run_name}: wrote {layout.results(run_name).name}")
    return measured.answers


def score(layout: Layout, clock: Clock, captions, run_names, depth: int) -> dict:
    cached = layout.caption_similarity.exists()
    with clock.span("judge_embedding"):
        similarity = judge.similarity(
            layout.judge_model,
            [c.caption_id for c in captions],
            [c.text for c in captions],
            layout.caption_similarity,
        )
    log(f"caption similarity: {'cached' if cached else 'computed'}")

    captions_by_track: dict[str, list[str]] = {}
    for caption in captions:
        captions_by_track.setdefault(caption.track_id, []).append(caption.caption_id)
    relevance = metrics.Relevance(similarity, captions_by_track)

    valid = {c.caption_id for c in captions if c.is_valid_subset}
    all_metrics = {}
    with clock.span("metrics"):
        for run_name in run_names:
            answers = retrieval.read_results(layout, run_name)
            subset = [a for a in answers if a.query_id in valid]
            all_metrics[run_name] = {
                "all": {
                    "strict": metrics.strict(answers, depth).as_dict(),
                    "graded": metrics.graded(answers, relevance).as_dict(),
                },
                "valid_subset": {
                    "strict": metrics.strict(subset, depth).as_dict(),
                    "graded": metrics.graded(subset, relevance).as_dict(),
                },
                "latency_ms": _latency(answers),
            }
            Path(layout.metrics(run_name)).write_text(
                json.dumps(all_metrics[run_name], indent=2)
            )
    return all_metrics


def _latency(answers) -> dict:
    values = sorted(a.latency_ms for a in answers)
    if not values:
        return {}
    return {
        "median": round(values[len(values) // 2], 1),
        "p95": round(values[int(len(values) * 0.95)], 1),
        "max": round(values[-1], 1),
        "mean": round(sum(values) / len(values), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="tmp/sdd_bench/latest",
        help="Where this run's artifacts go (default: tmp/sdd_bench/latest)",
    )
    parser.add_argument(
        "--top-k", type=int, default=50,
        help=(
            "Ranked depth to ask for (default: 50). It names the metrics "
            "and the result rows, so a resumed run must be given the same "
            "value as the retrieval it is scoring"
        ),
    )
    parser.add_argument(
        "--runs", nargs="*", default=list(RUNS), choices=RUNS,
        help="Which ranking configurations to query (default: both)",
    )
    parser.add_argument(
        "--stages", nargs="*",
        default=["prep", "index", "retrieve", "score", "report"],
        choices=["prep", "index", "retrieve", "score", "report", "mood"],
        help=(
            "Stages to run; earlier artifacts are reused when a stage is "
            "skipped. 'mood' is the MTG-Jamendo tag suite over the same "
            "index, and is not in the default set"
        ),
    )
    parser.add_argument(
        "--force-prep", action="store_true",
        help="Re-extract and re-strip the audio even if it is already there",
    )
    args = parser.parse_args()

    layout = Layout(out=Path(args.out).resolve()).ensure()
    clock = Clock(layout.out / "clock.json")
    log(f"artifacts: {layout.out}")

    stats = (
        prepare(layout, clock, args.force_prep)
        if "prep" in args.stages
        else json.loads(layout.dataset_stats.read_text())
    )
    captions = dataset.read_captions(layout.captions_csv)
    tracks = dataset.load_manifest(layout)

    instance = None
    summary = None
    try:
        if "index" in args.stages:
            instance, summary = index_library(layout, clock, args.top_k)
        elif "retrieve" in args.stages:
            instance = build(layout, mood=True, top_k=args.top_k)
            instance.start()

        if "retrieve" in args.stages:
            caption_queries = [
                retrieval.Query(c.caption_id, c.text, c.track_id) for c in captions
            ]
            ablation = MoodAblation(instance, clock, RUNS)
            for run_name in args.runs:
                ablation.prepare(run_name)
                retrieve(
                    instance, layout, clock, caption_queries, tracks, run_name,
                    args.top_k,
                )

    finally:
        if instance is not None:
            instance.stop()

    if "mood" in args.stages:
        mood.run_suite(layout, clock, tracks, log=log)

    if "score" in args.stages:
        score(layout, clock, captions, args.runs, args.top_k)

    if "report" in args.stages:
        with clock.span("report"):
            config = json.loads((layout.out / "server_config.json").read_text())
            fingerprint.collect(layout, config, stats)
            report.write(layout, clock, args.runs)
        log(f"report: {layout.report}")

    if (layout.out / "mood_metrics.json").exists() and {"mood", "report"} & set(
        args.stages
    ):
        log(f"mood report: {report.write_mood(layout, mood.RUNS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
