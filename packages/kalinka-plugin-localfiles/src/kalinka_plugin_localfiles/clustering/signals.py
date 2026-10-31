"""Membership signals — the evidence vocabulary for "do these tracks belong
to the same local album".

Pure functions over ``TrackFeatures``. They are not evaluated per track pair:
the folder pass forms candidate subgroups first and only scores between two
subgroups when a split is plausible. Positive score favours one album,
negative favours a split.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Signal weights (§5.2). Positive favours "same album", negative favours split.
WEIGHTS: Dict[str, int] = {
    "same_cue_sheet": 6,
    "same_art": 3,
    "compatible_track_numbers": 2,
    "same_album_tag": 2,
    "same_albumartist": 2,
    "same_stream": 1,
    "same_import_batch": 1,
    "track_number_collision": -3,
    "different_cue_sheets": -3,
    "different_art": -3,
    "different_album_tags_both_sequences": -3,
    "different_albumartist": -2,
    "disjoint_disc_sequences": -2,
    "stream_mismatch_with_tag_split": -1,
}

# Max Hamming distance (of a 64-bit dHash) for two covers to count as "same".
ART_SAME_MAX_HAMMING = 8
# Min distance for two covers to count as "clearly different".
ART_DIFF_MIN_HAMMING = 22


@dataclass
class TrackFeatures:
    """The per-track evidence the scorer reads (built by the engine in 1c)."""

    track_id: str
    album_key: Optional[str] = None       # normalized album tag
    album_from_cue: bool = False          # album_key is the cue title's
    albumartist_key: Optional[str] = None
    artist_key: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    art_phash: Optional[str] = None
    stream_key: Optional[str] = None      # codec|sr|bitdepth
    compilation: bool = False
    cue_sheet: Optional[str] = None
    import_batch: Optional[str] = None


@dataclass
class SequenceAnalysis:
    numbers: List[int] = field(default_factory=list)
    has_collision: bool = False
    is_plausible: bool = False   # covers >=60% of a contiguous 1..max run, >=4


def hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two equal-length hex strings (bit count)."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _dominant(values) -> Optional[str]:
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def sequence_analysis(numbers: List[Optional[int]]) -> SequenceAnalysis:
    """Analyse a set of track numbers for collisions and plausible ordering."""
    present = [n for n in numbers if n is not None and n > 0]
    if not present:
        return SequenceAnalysis()
    counts = Counter(present)
    has_collision = any(c > 1 for c in counts.values())
    top = max(present)
    distinct = len(counts)
    # Plausible if it looks like a real 1..n run: at least 4 tracks and the
    # distinct numbers cover >=60% of the 1..max range with no collisions.
    is_plausible = (
        not has_collision
        and distinct >= 4
        and top > 0
        and distinct / top >= 0.6
    )
    return SequenceAnalysis(
        numbers=sorted(present),
        has_collision=has_collision,
        is_plausible=is_plausible,
    )


def _named_by_cue(group: List[TrackFeatures]) -> bool:
    keyed = [t for t in group if t.album_key]
    return bool(keyed) and all(t.album_from_cue for t in keyed)


def _art_relation(a: List[TrackFeatures], b: List[TrackFeatures]) -> Optional[str]:
    """"same" / "different" / None from the closest cross-subgroup cover pair."""
    pa = [t.art_phash for t in a if t.art_phash]
    pb = [t.art_phash for t in b if t.art_phash]
    if not pa or not pb:
        return None
    best = min(hamming_hex(x, y) for x in pa for y in pb)
    if best <= ART_SAME_MAX_HAMMING:
        return "same"
    if best >= ART_DIFF_MIN_HAMMING:
        return "different"
    return None


def pair_signal_score(
    a: List[TrackFeatures], b: List[TrackFeatures]
) -> Tuple[int, Dict[str, int]]:
    """Signed weighted evidence that subgroups ``a`` and ``b`` are one album.

    Returns (score, basis) where basis maps each fired signal to its weight.
    """
    basis: Dict[str, int] = {}

    def fire(name: str) -> None:
        basis[name] = WEIGHTS[name]

    a_album = _dominant(t.album_key for t in a)
    b_album = _dominant(t.album_key for t in b)
    a_aa = _dominant(t.albumartist_key for t in a)
    b_aa = _dominant(t.albumartist_key for t in b)
    a_cue = _dominant(t.cue_sheet for t in a)
    b_cue = _dominant(t.cue_sheet for t in b)
    a_stream = _dominant(t.stream_key for t in a)
    b_stream = _dominant(t.stream_key for t in b)
    a_batch = _dominant(t.import_batch for t in a)
    b_batch = _dominant(t.import_batch for t in b)

    a_seq = sequence_analysis([t.track_number for t in a])
    b_seq = sequence_analysis([t.track_number for t in b])
    joint_seq = sequence_analysis([t.track_number for t in a + b])

    # "Tags differ" means both are present and distinct — a tagged-vs-untagged
    # pair is not a tag conflict.
    tags_differ = bool(a_album) and bool(b_album) and a_album != b_album

    if a_cue and a_cue == b_cue:
        fire("same_cue_sheet")
    elif tags_differ and _named_by_cue(a) and _named_by_cue(b):
        # Untagged discs whose cue sheets title them differently.
        fire("different_cue_sheets")

    art = _art_relation(a, b)
    if art == "same":
        fire("same_art")
    elif art == "different":
        fire("different_art")

    # Track numbers: a collision across the union (two "track 4"s) is split
    # evidence; a clean joint run is cohesion evidence.
    if joint_seq.has_collision:
        fire("track_number_collision")
    elif joint_seq.is_plausible:
        fire("compatible_track_numbers")

    if a_album and a_album == b_album:
        fire("same_album_tag")
    elif tags_differ and a_seq.is_plausible and b_seq.is_plausible:
        # Two distinct album tags that each stand on their own complete
        # sequence — the "two albums in one folder" signature.
        fire("different_album_tags_both_sequences")

    if a_aa and a_aa == b_aa:
        fire("same_albumartist")
    elif a_aa and b_aa and a_aa != b_aa:
        fire("different_albumartist")

    # Disjoint discs each with their own run and differing tags -> multi-disc
    # that should be handled as such rather than fused blindly.
    a_discs = {t.disc_number for t in a if t.disc_number}
    b_discs = {t.disc_number for t in b if t.disc_number}
    if (
        a_discs
        and b_discs
        and a_discs.isdisjoint(b_discs)
        and a_seq.is_plausible
        and b_seq.is_plausible
        and tags_differ
    ):
        fire("disjoint_disc_sequences")

    if a_stream and a_stream == b_stream:
        fire("same_stream")
    elif a_stream and b_stream and a_stream != b_stream and tags_differ:
        fire("stream_mismatch_with_tag_split")

    if a_batch and a_batch == b_batch:
        fire("same_import_batch")

    return sum(basis.values()), basis
