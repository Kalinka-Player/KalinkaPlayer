"""MTG-Jamendo mood/theme tags as ground truth for mood queries.

The captions benchmark asks whether search can find one recording from its
description. This one asks a different question: given a mood word, does the
ranking fill up with tracks a human tagged with that mood? Many tracks are
right for each query, which is what makes it the test the valence/arousal
work should be judged on — and what makes recall@k meaningless, so the
scoring moves to precision, mAP and nDCG over a relevance set.

The tags ship inside the Song Describer record itself, so this needs no
second download: every one of the 706 recordings joins to its MTG-Jamendo
row by the path the caption table already names.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .dataset import Track
from .paths import Layout

TAG_FILE = "song_describer_14_04_23.mtg-jamendo.tsv"

#: A mood/theme tag is not always a mood: roughly half of Jamendo's vocabulary
#: is what a library-music customer would license the track *for*. Fusion has
#: no reason to help on those, so they are asked and reported separately
#: rather than averaged into one number.
CONTEXTUAL = frozenset({
    "advertising", "background", "children", "christmas", "commercial",
    "communication", "corporate", "documentary", "drama", "fashion", "film",
    "game", "holiday", "nature", "party", "presentation", "sport", "summer",
    "trailer", "travel", "wedding", "entertainment",
})

#: Query wording, kept to what a person would type. A bare mood word is the
#: case the searcher's keyword spotting was built for; the compound is the
#: case its confidence weighting was built for.
SINGLE = "{mood} music"
COMPOUND = "{mood} {other}"


@dataclass(frozen=True)
class TagQuery:
    query_id: str
    text: str
    family: str
    relevant: frozenset[str]

    @property
    def support(self) -> int:
        return len(self.relevant)


def tag_file(layout: Layout) -> Path:
    return layout.dataset / TAG_FILE


def load_tags(path: Path) -> dict[str, dict[str, list[str]]]:
    """``{dataset path: {category: [tag, ...]}}`` for every track in the file."""
    tags: dict[str, dict[str, list[str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if len(row) < 6 or row[0] == "TRACK_ID":
                continue
            grouped: dict[str, list[str]] = {}
            for entry in row[5:]:
                if "---" not in entry:
                    continue
                category, _, name = entry.partition("---")
                grouped.setdefault(category, []).append(name)
            tags[row[3]] = grouped
    return tags


def judged_corpus(
    tracks: dict[str, Track], tags: dict[str, dict[str, list[str]]]
) -> frozenset[str]:
    """The tracks a mood question can be asked about: the ones somebody
    tagged with a mood at all.

    A track with no mood tag is not a track that is unmoved — it is a track
    nobody annotated, and counting it as a wrong answer would charge the
    ranking for a gap in the labels. It is dropped from the ranking before
    scoring instead, which is what the subset MTG publishes for this task
    does by construction.
    """
    return frozenset(
        track_id
        for track_id, track in tracks.items()
        if tags.get(track.source_path, {}).get("mood/theme")
    )


def _relevant(
    tracks: dict[str, Track],
    tags: dict[str, dict[str, list[str]]],
    wanted: dict[str, str],
) -> frozenset[str]:
    """Tracks carrying every (category, tag) pair in ``wanted``."""
    return frozenset(
        track_id
        for track_id, track in tracks.items()
        if all(
            tag in tags.get(track.source_path, {}).get(category, [])
            for category, tag in wanted.items()
        )
    )


def build_queries(
    tracks: dict[str, Track],
    tags: dict[str, dict[str, list[str]]],
    min_support: int = 10,
    min_compound: int = 8,
    max_compound: int = 12,
) -> list[TagQuery]:
    """One query per mood tag with enough tracks behind it, plus the mood +
    instrument/genre compounds that clear their own floor.

    A query with three relevant tracks measures nothing, so the floors are
    support floors, not a hand-picked list: change them and the query set
    changes with the corpus.
    """
    corpus = judged_corpus(tracks, tags)
    moods = Counter(
        mood
        for track_id in corpus
        for mood in tags[tracks[track_id].source_path]["mood/theme"]
    )

    queries: list[TagQuery] = []
    for mood, count in sorted(moods.items(), key=lambda kv: (-kv[1], kv[0])):
        if count < min_support:
            continue
        family = "contextual" if mood in CONTEXTUAL else "affective"
        queries.append(
            TagQuery(
                query_id=f"{family}:{mood}",
                text=SINGLE.format(mood=mood),
                family=family,
                relevant=_relevant(tracks, tags, {"mood/theme": mood}),
            )
        )

    pairs: Counter = Counter()
    for track_id in corpus:
        track_tags = tags[tracks[track_id].source_path]
        for mood in track_tags["mood/theme"]:
            if mood in CONTEXTUAL:
                continue
            for category in ("instrument", "genre"):
                for other in track_tags.get(category, []):
                    pairs[(mood, category, other)] += 1

    for (mood, category, other), count in pairs.most_common():
        if count < min_compound or len(
            [q for q in queries if q.family == "compound"]
        ) >= max_compound:
            continue
        queries.append(
            TagQuery(
                query_id=f"compound:{mood}+{category}:{other}",
                text=COMPOUND.format(mood=mood, other=other),
                family="compound",
                relevant=_relevant(
                    tracks, tags, {"mood/theme": mood, category: other}
                ),
            )
        )
    return queries


def write_queries(queries: Iterable[TagQuery], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["query_id", "query", "family", "support", "relevant_track_ids"])
        for query in queries:
            writer.writerow([
                query.query_id, query.text, query.family, query.support,
                ";".join(sorted(query.relevant)),
            ])
