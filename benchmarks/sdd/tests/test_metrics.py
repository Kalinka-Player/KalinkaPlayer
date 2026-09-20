"""The scoring, checked against rankings whose answers are known by hand."""

from sddbench import metrics
from sddbench.retrieval import MISS, Answer


def answer(caption_id: str, target: str, ranked: list[str]) -> Answer:
    return Answer(
        query_id=caption_id,
        target_track_id=target,
        ranked=ranked,
        scores=[None] * len(ranked),
        latency_ms=0.0,
    )


def test_rank_is_one_based_and_misses_are_far_away():
    assert answer("c1", "t1", ["t1", "t2"]).rank == 1
    assert answer("c1", "t2", ["t1", "t2"]).rank == 2
    assert answer("c1", "t9", ["t1", "t2"]).rank == MISS
    assert answer("c1", "t9", []).rank == MISS


def test_strict_counts_recall_mrr_and_median():
    answers = [
        answer("a", "t1", ["t1"]),                  # rank 1
        answer("b", "t2", ["t1", "t2"]),            # rank 2
        answer("c", "t3", ["t1", "t2", "t3"]),      # rank 3
        answer("d", "t9", ["t1"]),                  # miss
    ]
    strict = metrics.strict(answers, ks=(1, 5))
    assert strict.recall[1] == 0.25
    assert strict.recall[5] == 0.75
    assert round(strict.mrr, 4) == round((1 + 0.5 + 1 / 3) / 4, 4)
    assert strict.median_rank_hits == 2
    assert strict.found == 3
    assert strict.empty == 0


def test_empty_answers_are_counted_not_dropped():
    strict = metrics.strict([answer("a", "t1", []), answer("b", "t2", ["t2"])])
    assert strict.empty == 1
    assert strict.recall[1] == 0.5


def test_random_baseline_is_k_over_n():
    baseline = metrics.random_baseline(706, ks=(1, 10))
    assert baseline["recall_at"]["1"] == round(1 / 706, 4)
    assert baseline["recall_at"]["10"] == round(10 / 706, 4)


def test_ndcg_is_one_when_the_ranking_is_ideal():
    ideal = [1.0, 0.8, 0.2]
    assert metrics.ndcg_at_k([1.0, 0.8, 0.2], ideal, 3) == 1.0
    assert metrics.ndcg_at_k([0.2, 0.8, 1.0], ideal, 3) < 1.0
    assert metrics.ndcg_at_k([], ideal, 3) == 0.0


def test_relevance_is_the_best_caption_of_a_track():
    similarity = {"q": {"q": 1.0, "c1": 0.3, "c2": 0.9, "c3": 0.4}}
    relevance = metrics.Relevance.from_similarity(
        similarity, {"t1": ["c1", "c2"], "t2": ["c3"]}
    )
    assert relevance.of("q", "t1") == 0.9
    assert relevance.of("q", "t2") == 0.4
    assert sorted(relevance.all_for("q"), reverse=True) == [0.9, 0.4]


def test_graded_soft_recall_counts_a_threshold_clearing_hit():
    similarity = {
        "q": {"q": 1.0, "c_near": 0.65, "c_far": 0.1},
    }
    relevance = metrics.Relevance.from_similarity(
        similarity, {"target": ["q"], "near": ["c_near"], "far": ["c_far"]}
    )
    graded = metrics.graded(
        [answer("q", "target", ["near", "far"])], relevance, k=10,
        thresholds=(0.5, 0.7),
    )
    assert graded.soft_recall[0.5] == 1.0
    assert graded.soft_recall[0.7] == 0.0
    assert graded.mean_top1_relevance == 0.65
