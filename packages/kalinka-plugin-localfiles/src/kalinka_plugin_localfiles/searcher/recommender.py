"""Recommendations in CLAP audio space: what to queue next, and albums to try.

Both follow one recipe. A taste vector is the recency-weighted direction of
what the listener chose (Rocchio's positive centroid); each candidate scores
its likeness to it, minus its likeness to the nearest thing the listener
turned down; picks are then spread out by maximal marginal relevance, so one
album cannot fill every slot.

numpy is imported on call: the searcher installs it on demand before asking.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..embedding_utils import decode_embedding, encode_embedding
from .searcher_db import AsyncSearcherDb

#: How much a candidate's likeness to its nearest turned-down track counts against it.
AVOID_WEIGHT = 0.5
#: Share of a pick's score given to being unlike the picks before it.
DIVERSITY = 0.3
#: Above this likeness two vectors are the same recording, never both offered;
#: a candidate this like a turned-down track is not offered at all.
DUPLICATE_COS = 0.98
#: Each seed weighs this much of the one heard after it, so the queue follows
#: where the listening went.
SEED_DECAY = 0.5
#: The same for the play history, which describes a taste rather than a moment.
HISTORY_DECAY = 0.98
#: Nearest tracks considered before ranking.
TRACK_CANDIDATES = 200
#: Distinct heard-out tracks needed before albums come from the history.
MIN_HEARD_OUT = 5
#: Plays read from the history.
HISTORY_WINDOW = 300
#: Albums added last, standing in for a taste the history cannot give yet.
RECENT_ALBUMS = 3
#: The group of loose tracks, never an album to recommend.
LOOSE_TRACKS = "unknown_album"


def _unit(v):
    import numpy as np

    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


def taste(vectors: Sequence, decay: float):
    """The direction ``vectors`` share, oldest first; each weighs ``decay``
    times the one after it."""
    count = len(vectors)
    return _unit(sum(decay ** (count - 1 - i) * _unit(v) for i, v in enumerate(vectors)))


def pick(
    query,
    candidates: Mapping[str, object],
    avoid: Sequence,
    count: int,
    avoid_weight: float = AVOID_WEIGHT,
    diversity: float = DIVERSITY,
) -> list[str]:
    """The ``count`` candidates to offer, best first.

    @param query Unit taste vector.
    @param candidates Id to vector of what may be offered.
    @param avoid Vectors of what was turned down.
    @return Ids ranked by likeness to ``query``, less ``avoid_weight`` times
        likeness to the nearest of ``avoid``, re-ranked so each pick also
        differs from the ones before it. Never two copies of one recording,
        nor a copy of a turned-down one (see :data:`DUPLICATE_COS`).
    """
    import numpy as np

    ids = list(candidates)
    if not ids or count <= 0:
        return []
    vectors = np.stack([_unit(candidates[i]) for i in ids])
    score = vectors @ _unit(query)
    remaining = list(range(len(ids)))
    if len(avoid):
        likeness = (vectors @ np.stack([_unit(a) for a in avoid]).T).max(axis=1)
        score = score - avoid_weight * likeness
        remaining = [i for i in remaining if likeness[i] <= DUPLICATE_COS]
    nearest_pick = np.zeros(len(ids))
    chosen: list[int] = []
    while remaining and len(chosen) < count:
        mmr = (1 - diversity) * score[remaining] - diversity * nearest_pick[remaining]
        best = remaining.pop(int(np.argmax(mmr)))
        chosen.append(best)
        nearest_pick = np.maximum(nearest_pick, vectors @ vectors[best])
        remaining = [i for i in remaining if nearest_pick[i] <= DUPLICATE_COS]
    return [ids[i] for i in chosen]


class Recommender:
    """
    @brief Chooses tracks to carry a queue on with and albums for the
    listener, from the library's stored CLAP audio vectors.

    Reads the searcher's database only; owns no state between calls.
    """

    def __init__(self, db: AsyncSearcherDb):
        self.db = db

    async def next_tracks(
        self, seeds: Sequence[str], avoid: Sequence[str], exclude: Sequence[str], count: int
    ) -> list[str]:
        """Tracks that carry on from ``seeds`` (oldest first), never one of
        ``seeds``, ``avoid`` or ``exclude``, and unlike ``avoid``."""
        blobs = await self.db.get_track_vectors([*seeds, *avoid])
        heard = [decode_embedding(blobs[s]) for s in seeds if s in blobs]
        if not heard:
            return []
        query = taste(heard, SEED_DECAY)
        turned_down = [decode_embedding(blobs[a]) for a in avoid if a in blobs]
        skip = {*seeds, *avoid, *exclude}
        hits = await self.db.knn_search_audio(encode_embedding(query), TRACK_CANDIDATES + len(skip))
        wanted = [h["track_id"] for h in hits if h["track_id"] not in skip]
        found = await self.db.get_track_vectors(wanted)
        candidates = {t: decode_embedding(found[t]) for t in wanted if t in found}
        return pick(query, candidates, turned_down, count)

    async def albums_for_you(self, count: int) -> list[str]:
        """Albums near what the listener plays out and away from what they
        skip, none they have already heard out. With too short a history the
        albums added last stand in for the taste, and are not offered."""
        plays = await self.db.recent_plays(HISTORY_WINDOW)
        heard_out = list(dict.fromkeys(t for t, completed, _ in plays if completed))
        if len(heard_out) >= MIN_HEARD_OUT:
            skipped = [
                t for t in dict.fromkeys(t for t, _, skip in plays if skip) if t not in heard_out
            ]
            blobs = await self.db.get_track_vectors([*heard_out, *skipped])
            liked = [decode_embedding(blobs[t]) for t in reversed(heard_out) if t in blobs]
            disliked = [decode_embedding(blobs[t]) for t in skipped if t in blobs]
            known = await self.db.albums_of_tracks(heard_out)
        else:
            recent = await self.db.recently_added_albums(RECENT_ALBUMS)
            blobs = await self.db.get_album_vectors(recent)
            liked = [decode_embedding(blobs[a]) for a in reversed(recent) if a in blobs]
            disliked = []
            known = set(recent)
        if not liked:
            return []
        known.add(LOOSE_TRACKS)
        query = taste(liked, HISTORY_DECAY)
        hits = await self.db.knn_search_albums(encode_embedding(query), count * 4 + len(known))
        wanted = [h["album_id"] for h in hits if h["album_id"] not in known]
        found = await self.db.get_album_vectors(wanted)
        candidates = {a: decode_embedding(found[a]) for a in wanted if a in found}
        return pick(query, candidates, disliked, count)
