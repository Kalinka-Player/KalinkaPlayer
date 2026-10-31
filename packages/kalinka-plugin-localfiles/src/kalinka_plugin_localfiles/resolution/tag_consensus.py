"""Tag-consensus evidence source (Phase 2g, §6.5).

The genuine ``observed`` local value for an album's origin/era fields is what
its tracks' own tags agree on — read here from ``track_evidence.raw_tags``, not
proxied off the album row (which the indexer seeds from a single track, so it
goes NULL when that one file lacked the tag even though its siblings carry it).

Pure and requestless; handles both ID3 frames (``TDRC``/``TCON``, scalar) and
Vorbis comments (``date``/``genre``, lower-case and list-valued).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from ..utils.years import year_from_date

# Candidate tag keys per field, lower-cased (raw keys are matched case-insensitively).
_KEYS = {
    "year": ("tdrc", "tyer", "date", "year", "originaldate", "originalyear"),
    "genre": ("tcon", "genre"),
    "language": ("tlan", "language"),
}


def _first(value: Any) -> Any:
    # Vorbis comments arrive as single-element lists; ID3 frames as scalars.
    if isinstance(value, list):
        return value[0] if value else None
    return value


def extract_fields(raw_tags: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull ``{year, genre, language}`` (those present) from one track's tags."""
    lower = {k.lower(): v for k, v in (raw_tags or {}).items()}
    out: Dict[str, Any] = {}
    for field, keys in _KEYS.items():
        raw = next(
            (v for k in keys if (v := _first(lower.get(k))) not in (None, "")),
            None,
        )
        if raw is None:
            continue
        if field == "year":
            if (year := year_from_date(raw)) is not None:
                out["year"] = year
        else:
            out[field] = str(raw).strip()
    return out


def field_consensus(values: Iterable[Any]) -> Optional[Any]:
    """The dominant value among tracks, or None on emptiness or a tie. Text is
    grouped case-insensitively ('Rock'/'rock' don't split) and the most common
    original surface form of the winning group is returned; a plurality tie
    abstains, so a genuinely mixed album is left alone."""
    groups: Dict[Any, List[Any]] = {}
    for v in values:
        if v in (None, ""):
            continue
        key = v.casefold() if isinstance(v, str) else v
        groups.setdefault(key, []).append(v)
    if not groups:
        return None
    ranked = sorted(groups.values(), key=len, reverse=True)
    if len(ranked) > 1 and len(ranked[0]) == len(ranked[1]):
        return None  # tie -> abstain
    return Counter(ranked[0]).most_common(1)[0][0]


def album_tag_consensus(
    track_tags: Iterable[Optional[Dict[str, Any]]], fields: Iterable[str]
) -> Dict[str, Any]:
    """Consensus value per requested field across an album's tracks' raw tags."""
    fields = list(fields)
    collected: Dict[str, List[Any]] = {f: [] for f in fields}
    for raw in track_tags:
        present = extract_fields(raw)
        for f in fields:
            if f in present:
                collected[f].append(present[f])
    out: Dict[str, Any] = {}
    for f in fields:
        value = field_consensus(collected[f])
        if value is not None:
            out[f] = value
    return out
