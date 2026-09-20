"""Scoring a ranking against a set of right answers."""

from sddbench import metrics


def test_average_precision_rewards_early_hits():
    early = metrics.average_precision([True, True, False, False], 2)
    late = metrics.average_precision([False, False, True, True], 2)
    assert early == 1.0
    assert late < early


def test_average_precision_divides_by_the_corpus_not_the_list():
    # Three relevant exist; the ranking found one, at rank 1.
    assert metrics.average_precision([True, False], 3) == 1 / 3


def test_unjudged_tracks_are_dropped_before_scoring():
    score = metrics.score_set(
        "affective:happy", "affective",
        ranked=["a", "unjudged", "b"],
        relevant={"a", "b"},
        corpus={"a", "b", "c", "d"},
        k=2,
    )
    # "unjudged" is not in the corpus, so the condensed ranking is [a, b]:
    # both relevant, so P@2 is 1.0 rather than 0.5.
    assert score.judged == 2
    assert score.precision_at_10 == 1.0
    assert score.r_precision == 1.0
    assert score.average_precision == 1.0


def test_prevalence_and_lift_use_the_judged_corpus():
    score = metrics.score_set(
        "affective:happy", "affective",
        ranked=["a", "c", "d", "e"],
        relevant={"a", "b"},
        corpus={"a", "b", "c", "d", "e", "f", "g", "h", "i", "j"},
        k=10,
    )
    assert score.prevalence == 0.2
    assert score.precision_at_10 == 0.1
    assert score.lift == 0.5


def test_aggregate_summarises_per_family():
    scores = [
        metrics.score_set("a", "affective", ["x"], {"x"}, {"x", "y"}, k=1),
        metrics.score_set("b", "contextual", ["y"], {"x"}, {"x", "y"}, k=1),
    ]
    summary = metrics.aggregate(scores)
    assert summary["all"]["queries"] == 2
    assert summary["affective"]["mean_p_at_10"] == 1.0
    assert summary["contextual"]["mean_p_at_10"] == 0.0
