"""Stage 3 — every query asked of the public search endpoint, verbatim.

One query at a time, so a latency is a latency and not a queue. A caption
goes in exactly as the dataset wrote it, and a mood query exactly as the tag
names it: no cleaning, no truncation, no retry with a tidied string — what
the endpoint does with the words it is given is part of what is measured.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import httpx

from . import logparse
from .dataset import Track, kalinka_track_id
from .instance import KalinkaInstance
from .paths import Layout

MISS = 10**6  # rank standing for "not in the returned list at all"


@dataclass(frozen=True)
class Query:
    """What is asked, and (for the captions suite) the one track that is
    right. A mood query leaves the target empty: its answer is a set, and
    lives with the query set instead."""

    query_id: str
    text: str
    target_track_id: str = ""


@dataclass
class Answer:
    query_id: str
    target_track_id: str
    ranked: list[str]
    scores: list[Optional[float]]
    latency_ms: float
    error: str = ""

    @property
    def rank(self) -> int:
        """1-based rank of the target, or MISS."""
        try:
            return self.ranked.index(self.target_track_id) + 1
        except ValueError:
            return MISS


@dataclass
class QueryTiming:
    query_id: str
    server_total_s: float = 0.0
    encode_s: float = 0.0
    knn_s: float = 0.0
    mood_map_s: float = 0.0
    knn_hits: int = 0


def ask_raw(client: httpx.Client, query: str, source: str = "localfiles") -> dict:
    """One query's answer exactly as the endpoint serialised it."""
    response = client.get("/ai_search", params={"query": query, "sources": source})
    response.raise_for_status()
    return response.json()


def ranked_ids(payload: dict) -> list[str]:
    """The ids out of a suggestions card, in the order the source put them."""
    items = payload.get("items") or []
    if not items:
        return []
    return [section["id"] for section in items[0].get("sections") or []]


def ask(client: httpx.Client, query: str, source: str = "localfiles") -> list[str]:
    return ranked_ids(ask_raw(client, query, source))


def run(
    instance: KalinkaInstance,
    queries: Iterable[Query],
    tracks: dict[str, Track],
    layout: Layout,
    run_name: str,
    log=print,
) -> list[Answer]:
    """Ask every query, in order, one at a time."""
    by_kalinka_id = {
        kalinka_track_id(layout.audio / track.file): track_id
        for track_id, track in tracks.items()
    }
    client = httpx.Client(base_url=instance.base_url, timeout=120.0)
    answers: list[Answer] = []
    queries = list(queries)
    # One discarded query first. The mood index loads on the first query that
    # needs it, and the endpoint's per-call budget is three seconds — without
    # this the first query would be answered by a 503 that says nothing
    # about retrieval.
    try:
        ask(client, "warm up the query path")
    except Exception as exc:
        log(f"{run_name}: warm-up query failed: {exc!r}")
    started = time.time()
    for number, query in enumerate(queries, start=1):
        began = time.perf_counter()
        error = ""
        try:
            payload = ask_raw(client, query.text)
            ids = ranked_ids(payload)
            if number == 1:
                # Kept as the run's own evidence that the library carries no
                # text: the endpoint answers with hashes and Unknown Artist.
                (layout.out / f"sample_response_{run_name}.json").write_text(
                    json.dumps(payload, indent=2)[:200_000]
                )
        except httpx.HTTPStatusError as exc:
            # A 503 here is the server's own per-call budget giving up on the
            # source. It is a real answer a client would get, so it counts as
            # an empty one rather than being retried into a better number.
            ids, error = [], f"HTTP {exc.response.status_code}"
        except Exception as exc:  # a failed query is a result, not a crash
            ids, error = [], repr(exc)
        latency_ms = (time.perf_counter() - began) * 1000
        ranked = [by_kalinka_id[_local_id(entity)] for entity in ids
                  if _local_id(entity) in by_kalinka_id]
        unknown = len(ids) - len(ranked)
        if unknown:
            error = error or f"{unknown} id(s) outside the manifest"
        answers.append(
            Answer(
                query_id=query.query_id,
                target_track_id=query.target_track_id,
                ranked=ranked,
                scores=[None] * len(ranked),
                latency_ms=latency_ms,
                error=error,
            )
        )
        if number % 100 == 0:
            log(f"{run_name}: {number}/{len(queries)} queries")
    client.close()
    log(f"{run_name}: {len(answers)} queries in {time.time() - started:.1f}s")
    return answers


def _digest(text: str) -> str:
    """How a query is named in the log: the same short hash the searcher
    wrapper writes, so a line can be tied to the query that caused it."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _local_id(entity_id: str) -> str:
    """``kalinka:localfiles:track:track_abc`` -> ``track_abc``."""
    return entity_id.rsplit(":", 1)[-1]


def attach_scores(
    answers: list[Answer], queries: list[Query], log_path: Path, layout: Layout,
    tracks: dict[str, Track]
) -> dict:
    """Fill in each answer's CLAP distances from the searcher's own KNN leg.

    The endpoint publishes rank order and no score, so the distances come
    from the wrapper on the KNN call. Queries are serialised by the module's
    search lock, so a ``knn_audio`` line belongs to the ``query_total`` line
    that follows it; the query's digest on that line is what ties the pair to
    a caption rather than to a position in the file.
    """
    pending: dict[str, float] = {}
    per_digest: dict[str, list[dict[str, float]]] = {}
    for entry in logparse.entries(log_path):
        if not isinstance(entry, logparse.Bench):
            continue
        if entry.event == "knn_audio":
            pending = _distances(entry.fields.get("dist", ""))
        elif entry.event == "query_total":
            per_digest.setdefault(entry.fields.get("q", ""), []).append(pending)
            pending = {}

    used: dict[str, int] = {}
    matched = 0
    for answer, query in zip(answers, queries):
        digest = _digest(query.text)
        seen = used.get(digest, 0)
        options = per_digest.get(digest, [])
        distances = options[seen] if seen < len(options) else {}
        used[digest] = seen + 1
        if distances:
            matched += 1
        answer.scores = [
            distances.get(_kalinka_of(track_id, tracks, layout)) for track_id in answer.ranked
        ]
    returned = sum(len(answer.ranked) for answer in answers)
    scored = sum(
        1 for answer in answers for score in answer.scores if score is not None
    )
    return {
        "queries": len(answers),
        "queries_with_distances": matched,
        "returned_tracks": returned,
        # With the mood leg off, every returned track came from the KNN leg,
        # so anything below 1.0 means a query was answered with another
        # query's hits — worth failing the run over, not rounding away.
        "share_with_clap_distance": round(scored / returned, 4) if returned else 0.0,
    }


def _kalinka_of(track_id: str, tracks: dict[str, Track], layout: Layout) -> str:
    return kalinka_track_id(layout.audio / tracks[track_id].file)


def _distances(dump: str) -> dict[str, float]:
    if not dump or dump == "-":
        return {}
    out: dict[str, float] = {}
    for pair in dump.split(";"):
        track, _, value = pair.partition(":")
        try:
            out[track] = float(value)
        except ValueError:
            continue
    return out


def query_timings(log_path: Path, queries: list[Query]) -> list[QueryTiming]:
    """The server-side split of each query, aligned the same way as scores."""
    per_digest: dict[str, list[dict[str, float]]] = {}
    current: dict[str, float] = {}
    for entry in logparse.entries(log_path):
        if not isinstance(entry, logparse.Bench):
            continue
        if entry.event == "query_encode":
            current["encode_s"] = entry.number("t")
        elif entry.event == "query_knn":
            current["knn_s"] = entry.number("t")
            current["knn_hits"] = entry.number("hits")
        elif entry.event == "query_mood_map":
            current["mood_map_s"] = entry.number("t")
        elif entry.event == "query_total":
            current["server_total_s"] = entry.number("t")
            per_digest.setdefault(entry.fields.get("q", ""), []).append(current)
            current = {}

    used: dict[str, int] = {}
    timings: list[QueryTiming] = []
    for query in queries:
        digest = _digest(query.text)
        seen = used.get(digest, 0)
        options = per_digest.get(digest, [])
        found = options[seen] if seen < len(options) else {}
        used[digest] = seen + 1
        timings.append(
            QueryTiming(
                query_id=query.query_id,
                server_total_s=found.get("server_total_s", 0.0),
                encode_s=found.get("encode_s", 0.0),
                knn_s=found.get("knn_s", 0.0),
                mood_map_s=found.get("mood_map_s", 0.0),
                knn_hits=int(found.get("knn_hits", 0)),
            )
        )
    return timings


def write_results(
    answers: list[Answer], timings: list[QueryTiming], layout: Layout,
    run_name: str, id_column: str = "caption_id",
) -> None:
    with layout.results(run_name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            id_column, "target_track_id", "rank", "n_results",
            "latency_ms", "server_total_s", "encode_s", "knn_s", "mood_map_s",
            "ranked_track_ids", "clap_distances", "error",
        ])
        for answer, timing in zip(answers, timings):
            writer.writerow([
                answer.query_id,
                answer.target_track_id,
                answer.rank if answer.rank != MISS else ">50",
                len(answer.ranked),
                round(answer.latency_ms, 2),
                round(timing.server_total_s, 4),
                round(timing.encode_s, 4),
                round(timing.knn_s, 4),
                round(timing.mood_map_s, 4),
                ";".join(answer.ranked),
                ";".join("" if score is None else f"{score:.6f}" for score in answer.scores),
                answer.error,
            ])


def read_results(
    layout: Layout, run_name: str, id_column: str = "caption_id"
) -> list[Answer]:
    answers: list[Answer] = []
    with layout.results(run_name).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ranked = [tid for tid in row["ranked_track_ids"].split(";") if tid]
            scores = [
                float(value) if value else None
                for value in row["clap_distances"].split(";")
            ] if row["clap_distances"] else []
            answers.append(
                Answer(
                    query_id=row[id_column],
                    target_track_id=row["target_track_id"],
                    ranked=ranked,
                    scores=scores + [None] * (len(ranked) - len(scores)),
                    latency_ms=float(row["latency_ms"]),
                    error=row["error"],
                )
            )
    return answers
