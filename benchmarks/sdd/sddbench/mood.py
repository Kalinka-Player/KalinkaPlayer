"""The mood suite — the one that puts the valence/arousal work on trial.

A caption names one recording, so the captions suite measures CLAP doing
what CLAP is best at. A mood word names dozens, and mood is the axis CLAP is
weakest on, which is why the fusion exists. Here a query is a tag, every
track a human gave that tag is a right answer, and the score is precision,
mAP and nDCG over that set — recall@k means nothing when forty answers are
correct.

Scored over judged tracks only: a recording nobody tagged is dropped from
the ranking rather than counted against it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Sequence

from . import metrics, retrieval, tags
from .clock import Clock
from .dataset import Track
from .instance import KalinkaInstance, MoodAblation, build
from .paths import Layout

#: Each ranking configuration, and the valence/arousal setting it is named
#: for — see ``run.RUNS``.
RUNS = {"tags_fusion": True, "tags_clap": False}
RUN_TITLES = {
    "tags_fusion": "CLAP + valence/arousal fusion (shipped default)",
    "tags_clap": "CLAP only (mood ranking off)",
}
FAMILY_TITLES = {
    "affective": "Affective — happy, sad, relaxing, dreamy …",
    "contextual": "Contextual — commercial, documentary, children …",
    "compound": "Compound — a mood and a sound, e.g. \"emotional piano\"",
}


def queries(layout: Layout, tracks: dict[str, Track]):
    """The query set and the judged corpus behind it."""
    tag_rows = tags.load_tags(tags.tag_file(layout))
    corpus = tags.judged_corpus(tracks, tag_rows)
    return tags.build_queries(tracks, tag_rows), corpus


def score(
    answers: Sequence[retrieval.Answer],
    query_set: Sequence[tags.TagQuery],
    corpus,
) -> list[metrics.SetScore]:
    by_id = {query.query_id: query for query in query_set}
    return [
        metrics.score_set(
            answer.query_id,
            by_id[answer.query_id].family,
            answer.ranked,
            by_id[answer.query_id].relevant,
            corpus,
        )
        for answer in answers
        if answer.query_id in by_id
    ]


def write_scores(scores: Sequence[metrics.SetScore], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(scores[0].as_dict()) if scores else ["query_id"]
        )
        writer.writeheader()
        for score_row in scores:
            writer.writerow(score_row.as_dict())


def run_suite(
    layout: Layout,
    clock: Clock,
    tracks: dict[str, Track],
    runs: Iterable[str] = RUNS,
    log=print,
) -> dict:
    """Ask every mood query under each ranking configuration.

    The ranked depth is the whole library here, not the shipped 50: mAP and
    R-precision are defined over a complete ranking, and a list cut at 50
    would report the cut rather than the ranking. Nothing else is changed,
    and the cut is what the captions suite measures.
    """
    query_set, corpus = queries(layout, tracks)
    log(f"mood suite: {len(query_set)} queries over {len(corpus)} judged tracks")
    tags.write_queries(query_set, layout.out / "mood_queries.csv")
    asked = [retrieval.Query(q.query_id, q.text) for q in query_set]

    instance: KalinkaInstance = build(
        layout, mood=True, top_k=len(tracks), candidates=len(tracks)
    )
    results: dict = {"judged_tracks": len(corpus), "queries": len(query_set)}
    try:
        instance.start()
        ablation = MoodAblation(instance, clock, RUNS)
        for run_name in runs:
            ablation.prepare(run_name)
            measured = retrieval.measure(
                instance, clock, asked, tracks, layout, run_name, log=log
            )
            retrieval.write_results(
                measured.answers, measured.timings, layout, run_name,
                len(tracks), id_column="query_id",
            )
            scores = score(measured.answers, query_set, corpus)
            write_scores(scores, layout.out / f"mood_scores_{run_name}.csv")
            results[run_name] = {
                "aggregate": metrics.aggregate(scores),
                "per_query": [s.as_dict() for s in scores],
            }
            log(f"{run_name}: {json.dumps(results[run_name]['aggregate']['all'])}")
    finally:
        instance.stop()

    (layout.out / "mood_metrics.json").write_text(json.dumps(results, indent=2))
    return results
