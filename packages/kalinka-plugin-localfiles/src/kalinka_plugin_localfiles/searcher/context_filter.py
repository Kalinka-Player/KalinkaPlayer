"""The conditions that keep Smart Search and Keep playing from offering the
wrong kind of track, however alike it sounds.

CLAP places a track by its sound alone. Whether a Spanish ballad may follow an
English pop song, or a Christmas standard play in March, is not in the sound:
it is read from the library's metadata and applied before ranking.

A search's facets are SQL conditions, as they narrow the whole library; the
rules a queue's seeds set are checked here, on the few hundred tracks nearest
the seeds, where telling holiday music by its titles is cheap.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from ..utils.years import album_year_sql
from .languages import canonical_language, language_spellings
from .query_terms import Origin, decades_in, origins_in

#: The rows a facet's condition reads.
TRACK_FACTS = (
    "tracks t LEFT JOIN albums a ON a.id = t.album_id LEFT JOIN artists ar ON ar.id = t.artist_id"
)
#: A track's language as its tags or its album's name it, over :data:`TRACK_FACTS`.
TRACK_LANGUAGE = "COALESCE(NULLIF(trim(t.language), ''), trim(a.language))"
_COUNTRY = "upper(trim(ar.country))"


def _marks(values: Sequence) -> str:
    return ",".join("?" * len(values))


class Facet(Protocol):
    """A condition a search names, as SQL over :data:`TRACK_FACTS`."""

    def matches(self) -> tuple[str, list]:
        """The condition a track meets, and its parameters."""
        ...

    def known(self) -> str:
        """The condition a track meets when it carries what this facet
        reads: a library where none does cannot apply the facet."""
        ...


@dataclass(frozen=True)
class FromDecades:
    """Tracks whose album first came out in one of ``decades``, each named by its first year."""

    decades: tuple[int, ...]

    def matches(self) -> tuple[str, list]:
        year = album_year_sql("a")
        clause = " OR ".join(f"{year} BETWEEN ? AND ?" for _ in self.decades)
        return clause, [y for decade in self.decades for y in (decade, decade + 9)]

    def known(self) -> str:
        return f"{album_year_sql('a')} IS NOT NULL"


@dataclass(frozen=True)
class FromOrigins:
    """Tracks sung in one of the origins' languages, or by an artist from one of their countries."""

    origins: tuple[Origin, ...]

    def matches(self) -> tuple[str, list]:
        spellings = [s for o in self.origins if o.language for s in language_spellings(o.language)]
        countries = sorted({c for o in self.origins for c in o.countries})
        clauses = []
        if spellings:
            clauses.append(f"{TRACK_LANGUAGE} IN ({_marks(spellings)})")
        if countries:
            clauses.append(f"{_COUNTRY} IN ({_marks(countries)})")
        return " OR ".join(clauses), [*spellings, *countries]

    def known(self) -> str:
        read = []
        if any(origin.language for origin in self.origins):
            read.append(f"{TRACK_LANGUAGE} != ''")
        if any(origin.countries for origin in self.origins):
            read.append(f"{_COUNTRY} != ''")
        return f"({' OR '.join(read)})"


def facets_of(query: str) -> list[Facet]:
    """The decades and origins ``query`` names, as facets of the search."""
    facets: list[Facet] = []
    if decades := decades_in(query):
        facets.append(FromDecades(tuple(decades)))
    if origins := origins_in(query):
        facets.append(FromOrigins(tuple(origins)))
    return facets


_SEASON_WORDS = (
    "christmas", "xmas", "x-mas", "noël", "navidad", "weihnacht",
    "рождеств", "новогод", "jingle bell", "santa claus",
)
_SEASON = re.compile("|".join(map(re.escape, _SEASON_WORDS)))
# "Holiday" names the season only as a genre: Madonna's and Billie Holiday's are not seasonal.
_HOLIDAY_GENRE = re.compile(rf"\bholiday\b|{_SEASON.pattern}")


def _folded(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _is_holiday(genre: Optional[str], album_title: Optional[str], title: Optional[str]) -> bool:
    if genre and _HOLIDAY_GENRE.search(_folded(genre)):
        return True
    return any(text and _SEASON.search(_folded(text)) for text in (album_title, title))


@dataclass(frozen=True)
class TrackContext:
    """What a queue's rules read of a track: the ISO 639-3 code of the
    language it is sung in, None where unknown, and whether it is holiday music."""

    language: Optional[str] = None
    holiday: bool = False

    @classmethod
    def read(
        cls,
        language: Optional[str],
        genre: Optional[str],
        album_title: Optional[str],
        title: Optional[str],
    ) -> "TrackContext":
        """The context of a track whose tags and enrichment carry these."""
        return cls(canonical_language(language), _is_holiday(genre, album_title, title))


class Constraint(Protocol):
    """A condition a track must meet to carry a queue on."""

    def admits(self, track: TrackContext) -> bool: ...


@dataclass(frozen=True)
class InLanguages:
    """Tracks sung in one of ``languages``, or in none the library knows."""

    languages: frozenset[str]

    def admits(self, track: TrackContext) -> bool:
        return track.language is None or track.language in self.languages


@dataclass(frozen=True)
class NotHoliday:
    """Anything but holiday music."""

    def admits(self, track: TrackContext) -> bool:
        return not track.holiday


def seed_rules(seeds: Iterable[TrackContext]) -> list[Constraint]:
    """What a track carrying a queue on from ``seeds`` must meet.

    It is sung in a language one of the seeds is sung in, where any seed's
    is known, and it is not holiday music unless one of the seeds is. A track
    whose language is unknown can follow any seed: only a known mismatch is
    the wrong kind of track.
    """
    seeds = list(seeds)
    rules: list[Constraint] = []
    if languages := {seed.language for seed in seeds if seed.language}:
        rules.append(InLanguages(frozenset(languages)))
    if not any(seed.holiday for seed in seeds):
        rules.append(NotHoliday())
    return rules


def admitted(tracks: Mapping[str, TrackContext], constraints: Sequence[Constraint]) -> list[str]:
    """The ids of ``tracks`` that every one of ``constraints`` admits, in order."""
    return [
        track_id
        for track_id, context in tracks.items()
        if all(constraint.admits(context) for constraint in constraints)
    ]
