"""Stage 4 and 5 — what the answers are worth.

Two scores of the same ranking, deliberately disagreeing. The strict one
counts only the recording the caption was written about, which under-counts
every honest near-miss the dataset never labelled. The graded one asks a
sentence-embedding judge how close each returned track's own captions are to
the query, which over-counts whenever the judge mistakes shared vocabulary
for shared music. Neither is the truth; the pair brackets it.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Collection, Iterable, Optional, Sequence

from .retrieval import MISS, Answer

STRICT_KS = (1, 5, 10, 50)
SOFT_THRESHOLDS = (0.5, 0.6, 0.7)
NDCG_K = 10


@dataclass
class Strict:
    n: int
    recall: dict[int, float]
    mrr: float
    median_rank: Optional[float]
    median_rank_hits: Optional[float]
    found: int
    empty: int

    def as_dict(self) -> dict:
        return {
            "queries": self.n,
            "recall_at": {str(k): round(v, 4) for k, v in self.recall.items()},
            "mrr": round(self.mrr, 4),
            "median_rank": self.median_rank,
            "median_rank_of_found": self.median_rank_hits,
            "found_in_top_50": self.found,
            "empty_answers": self.empty,
        }


def strict(answers: Sequence[Answer], ks: Iterable[int] = STRICT_KS) -> Strict:
    ranks = [answer.rank for answer in answers]
    if not ranks:
        return Strict(0, {k: 0.0 for k in ks}, 0.0, None, None, 0, 0)
    found = [rank for rank in ranks if rank != MISS]
    return Strict(
        n=len(ranks),
        recall={k: sum(1 for r in ranks if r <= k) / len(ranks) for k in ks},
        mrr=sum(0.0 if r == MISS else 1.0 / r for r in ranks) / len(ranks),
        median_rank=statistics.median(ranks) if ranks else None,
        median_rank_hits=statistics.median(found) if found else None,
        found=len(found),
        empty=sum(1 for answer in answers if not answer.ranked),
    )


def random_baseline(n_tracks: int, ks: Iterable[int] = STRICT_KS) -> dict:
    """A shuffled library's expectation: the target is one of ``n_tracks``,
    so it lands in the top k with probability k/n, and the MRR is the mean of
    1/rank over a uniform rank."""
    ks = list(ks)
    return {
        "recall_at": {str(k): round(min(1.0, k / n_tracks), 4) for k in ks},
        "mrr_top_50": round(
            sum(1.0 / rank for rank in range(1, 51)) / n_tracks, 4
        ),
    }


@dataclass
class Graded:
    n: int
    ndcg_at_10: float
    soft_recall: dict[float, float]
    mean_top1_relevance: float

    def as_dict(self) -> dict:
        return {
            "queries": self.n,
            "ndcg_at_10": round(self.ndcg_at_10, 4),
            "soft_recall_at_10": {
                str(t): round(v, 4) for t, v in self.soft_recall.items()
            },
            "mean_top1_relevance": round(self.mean_top1_relevance, 4),
        }


def dcg(relevances: Sequence[float]) -> float:
    return sum(rel / math.log2(position + 1) for position, rel in enumerate(relevances, 1))


def ndcg_at_k(ranked_relevance: Sequence[float], ideal: Sequence[float], k: int) -> float:
    best = dcg(sorted(ideal, reverse=True)[:k])
    return dcg(list(ranked_relevance)[:k]) / best if best > 0 else 0.0


def graded(
    answers: Sequence[Answer],
    relevance: "Relevance",
    k: int = NDCG_K,
    thresholds: Iterable[float] = SOFT_THRESHOLDS,
) -> Graded:
    """nDCG@k and soft recall against the judge's caption similarities.

    A track's relevance to a query is the highest cosine between the query
    caption and any caption that track has — including the query's own, which
    is why the target track always scores 1.0 and the ideal ranking is the
    one that puts it first.
    """
    thresholds = list(thresholds)
    ndcgs: list[float] = []
    hits = {threshold: 0 for threshold in thresholds}
    top1: list[float] = []
    for answer in answers:
        scored = [relevance.of(answer.query_id, track) for track in answer.ranked[:k]]
        ideal = relevance.all_for(answer.query_id)
        ndcgs.append(ndcg_at_k(scored, ideal, k))
        best = max(scored, default=0.0)
        top1.append(scored[0] if scored else 0.0)
        for threshold in thresholds:
            if best >= threshold:
                hits[threshold] += 1
    n = len(answers)
    return Graded(
        n=n,
        ndcg_at_10=sum(ndcgs) / n if n else 0.0,
        soft_recall={t: hits[t] / n if n else 0.0 for t in thresholds},
        mean_top1_relevance=sum(top1) / n if n else 0.0,
    )


class Relevance:
    """caption -> track relevance, as the max cosine over that track's captions.

    Holds one row per query caption over all tracks, so ``all_for`` can hand
    the ideal ranking to nDCG without recomputing anything.
    """

    def __init__(self, rows: dict[str, dict[str, float]]):
        self._rows = rows

    @classmethod
    def from_similarity(
        cls,
        similarity: dict[str, dict[str, float]],
        captions_by_track: dict[str, list[str]],
    ) -> "Relevance":
        rows: dict[str, dict[str, float]] = {}
        for caption_id, per_caption in similarity.items():
            rows[caption_id] = {
                track: max(per_caption.get(other, 0.0) for other in others)
                for track, others in captions_by_track.items()
            }
        return cls(rows)

    def of(self, caption_id: str, track_id: str) -> float:
        return self._rows.get(caption_id, {}).get(track_id, 0.0)

    def all_for(self, caption_id: str) -> list[float]:
        return list(self._rows.get(caption_id, {}).values())


# ---------------------------------------------------------------------------
# Set relevance — many right answers per query
# ---------------------------------------------------------------------------


@dataclass
class SetScore:
    """One query scored against a set of relevant tracks."""

    query_id: str
    family: str
    support: int
    judged: int
    precision_at_10: float
    average_precision: float
    ndcg_at_10: float
    r_precision: float
    prevalence: float

    @property
    def lift(self) -> float:
        """Precision@10 over what picking at random would have given."""
        return self.precision_at_10 / self.prevalence if self.prevalence else 0.0

    def as_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "family": self.family,
            "support": self.support,
            "judged": self.judged,
            "p_at_10": round(self.precision_at_10, 4),
            "average_precision": round(self.average_precision, 4),
            "ndcg_at_10": round(self.ndcg_at_10, 4),
            "r_precision": round(self.r_precision, 4),
            "prevalence": round(self.prevalence, 4),
            "lift": round(self.lift, 2),
        }


def average_precision(hits: Sequence[bool], total_relevant: int) -> float:
    """Mean of the precisions at each rank that holds a relevant track.

    Divided by the number of relevant tracks in the corpus, not by the number
    the ranking happened to find, so a ranking that returns three of forty
    cannot score like one that returns three of three.
    """
    if not total_relevant:
        return 0.0
    found = 0
    running = 0.0
    for position, hit in enumerate(hits, start=1):
        if hit:
            found += 1
            running += found / position
    return running / total_relevant


def score_set(
    query_id: str,
    family: str,
    ranked: Sequence[str],
    relevant: Collection[str],
    corpus: Collection[str],
    k: int = NDCG_K,
) -> SetScore:
    """Score one ranking against a relevance set, over judged tracks only.

    Tracks nobody tagged are dropped from the ranking before scoring rather
    than counted as mistakes — the labels are incomplete, and charging the
    ranking for that measures the annotation, not the search.
    """
    judged = [track for track in ranked if track in corpus]
    hits = [track in relevant for track in judged]
    ideal = [True] * min(len(relevant), k)
    prevalence = len(relevant) / len(corpus) if corpus else 0.0
    return SetScore(
        query_id=query_id,
        family=family,
        support=len(relevant),
        judged=len(judged),
        precision_at_10=sum(hits[:k]) / k if k else 0.0,
        average_precision=average_precision(hits, len(relevant)),
        ndcg_at_10=ndcg_at_k(
            [1.0 if hit else 0.0 for hit in hits],
            [1.0] * len(ideal),
            k,
        ),
        r_precision=(
            sum(hits[: len(relevant)]) / len(relevant) if relevant else 0.0
        ),
        prevalence=prevalence,
    )


def aggregate(scores: Sequence[SetScore]) -> dict:
    """The suite's headline numbers, and the same per family."""

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def summarise(subset: Sequence[SetScore]) -> dict:
        return {
            "queries": len(subset),
            "mean_p_at_10": round(mean([s.precision_at_10 for s in subset]), 4),
            "map": round(mean([s.average_precision for s in subset]), 4),
            "mean_ndcg_at_10": round(mean([s.ndcg_at_10 for s in subset]), 4),
            "mean_r_precision": round(mean([s.r_precision for s in subset]), 4),
            "mean_prevalence": round(mean([s.prevalence for s in subset]), 4),
            "mean_lift": round(mean([s.lift for s in subset]), 2),
        }

    families = sorted({score.family for score in scores})
    return {
        "all": summarise(scores),
        **{
            family: summarise([s for s in scores if s.family == family])
            for family in families
        },
    }
