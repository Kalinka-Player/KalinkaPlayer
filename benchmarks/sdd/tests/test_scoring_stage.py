"""The scoring stage: what it reads back, what it filters, what it publishes."""

import json

import run
from sddbench import judge, retrieval
from sddbench.clock import Clock
from sddbench.dataset import Caption
from sddbench.paths import Layout


def _seeded(tmp_path) -> tuple[Layout, list[Caption]]:
    """One finished retrieval run and a cached judge matrix, which is all the
    scoring stage reads — it never needs the server or the judge model."""
    layout = Layout(out=tmp_path)
    captions = [
        Caption("c1", "t1", "a piano piece", True),
        Caption("c2", "t2", "a drum solo", False),
    ]
    judge.write_matrix(
        [[1.0, 0.2], [0.2, 1.0]], ["c1", "c2"], layout.caption_similarity
    )
    retrieval.write_results(
        [
            retrieval.Answer("c1", "t1", ["t1", "t2"], [0.1, 0.2], 10.0),
            retrieval.Answer("c2", "t2", ["t1"], [0.3], 12.0),
        ],
        [retrieval.QueryTiming("c1"), retrieval.QueryTiming("c2")],
        layout, "mood_on", 50,
    )
    return layout, captions


def test_a_run_is_scored_over_every_caption_and_over_the_validated_subset(tmp_path):
    layout, captions = _seeded(tmp_path)
    scored = run.score(
        layout, Clock(tmp_path / "clock.json"), captions, ["mood_on"], 50
    )
    strict = scored["mood_on"]["all"]["strict"]
    assert strict["queries"] == 2
    assert strict["recall_at"]["1"] == 0.5
    subset = scored["mood_on"]["valid_subset"]["strict"]
    assert subset["queries"] == 1  # c1 alone is flagged, and it ranked first
    assert subset["recall_at"]["1"] == 1.0
    assert json.loads(layout.metrics("mood_on").read_text()) == scored["mood_on"]


def test_the_metrics_carry_the_depth_the_run_was_asked_at(tmp_path):
    layout, captions = _seeded(tmp_path)
    scored = run.score(
        layout, Clock(tmp_path / "clock.json"), captions, ["mood_on"], 20
    )
    strict = scored["mood_on"]["all"]["strict"]
    assert strict["depth"] == 20
    assert "50" not in strict["recall_at"]
