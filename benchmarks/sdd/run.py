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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sddbench import (
    dataset, fingerprint, indexing, judge, metrics, mood, report, retrieval,
)
from sddbench.clock import Clock
from sddbench.instance import build, set_mood
from sddbench.paths import Layout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sdd-bench").info

RUNS = ("mood_on", "mood_off")


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
             id_column: str = "caption_id"):
    mark = instance.log_path.stat().st_size
    with clock.span(f"retrieval_{run_name}"):
        answers = retrieval.run(
            instance, queries, tracks, layout, run_name, log=log
        )
    time.sleep(3)  # let the log queue drain before the slice is read back
    slice_path = layout.server_log(run_name)
    with instance.log_path.open("rb") as handle:
        handle.seek(mark)
        slice_path.write_bytes(handle.read())
    coverage = retrieval.attach_scores(
        answers, list(queries), slice_path, layout, tracks
    )
    timings = retrieval.query_timings(slice_path, list(queries))
    retrieval.write_results(answers, timings, layout, run_name, id_column)
    (layout.out / f"coverage_{run_name}.json").write_text(json.dumps(coverage, indent=2))
    log(f"{run_name}: {json.dumps(coverage)}")
    log(f"{run_name}: wrote {layout.results(run_name).name}")
    return answers


def score(layout: Layout, clock: Clock, captions, run_names) -> dict:
    caption_ids = [c.caption_id for c in captions]
    with clock.span("judge_embedding"):
        if layout.caption_similarity.exists():
            log("caption similarity: cached")
            similarity = judge.read_matrix(layout.caption_similarity)
        else:
            judging = judge.Judge(layout.judge_model)
            matrix = judge.similarity_matrix(
                judging, caption_ids, [c.text for c in captions]
            )
            judge.write_matrix(matrix, caption_ids, layout.caption_similarity)
            similarity = {
                caption_ids[i]: {
                    caption_ids[j]: float(matrix[i][j]) for j in range(len(caption_ids))
                }
                for i in range(len(caption_ids))
            }

    captions_by_track: dict[str, list[str]] = {}
    for caption in captions:
        captions_by_track.setdefault(caption.track_id, []).append(caption.caption_id)
    relevance = metrics.Relevance.from_similarity(similarity, captions_by_track)

    valid = {c.caption_id for c in captions if c.is_valid_subset}
    all_metrics = {}
    with clock.span("metrics"):
        for run_name in run_names:
            answers = retrieval.read_results(layout, run_name)
            subset = [a for a in answers if a.caption_id in valid]
            all_metrics[run_name] = {
                "all": {
                    "strict": metrics.strict(answers).as_dict(),
                    "graded": metrics.graded(answers, relevance).as_dict(),
                },
                "valid_subset": {
                    "strict": metrics.strict(subset).as_dict(),
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
        "--top-k", type=int, default=50, help="Ranked depth to ask for (default: 50)",
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
            for run_name in args.runs:
                if run_name == "mood_off":
                    with clock.span("ablation_restart"):
                        set_mood(instance, False)
                retrieve(instance, layout, clock, caption_queries, tracks, run_name)

    finally:
        if instance is not None:
            instance.stop()

    if "mood" in args.stages:
        mood.run_suite(layout, clock, tracks, log=log)

    if "score" in args.stages:
        score(layout, clock, captions, args.runs)

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
