"""One search's name hits, ranked.

A source that ranks its own hits (``ModuleCapability.RANKED_SEARCH``) keeps
its order: a streaming service weighs relevance and popularity across a
catalogue that no name comparison can see into, and its exact-name hits
include profiles nobody listens to. A source that lists hits by name instead
(the library, collections) is ordered by how each name answers the query: by
tier first — exactly, once an edition suffix is set aside, a typo away, as a
fragment, through its artist or album, or barely — then by a whole-string
similarity with a penalty for words the query never asked for, so "The
Beatles" leads "The Beatles 1962–1966" even though both contain the query.

Kinds and sources then merge by reciprocal rank: a hit's relevance is
1 / (1 + its place among its source's hits of that kind), which needs no
score comparable between services. Ties go to the less granular kind, so the
list opens with the best artist, album, playlist and track rather than a run
of one kind, and then to the earlier source. Nothing is ever cut: the sources
chose what to return, ranking only decides what leads.

Every hit is annotated with its tier, score and relevance, which is what lets
a client holding several sources' listings merge them by relevance without
ranking anything itself.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EmptyList,
    EntityType,
    MatchTier,
    NameMatch,
)
from kalinka_plugin_sdk.inputmodule import ModuleCapability, SearchType

from .browse_source import BrowseSource
from .config_model import SearchConfig
from .module_capabilities import capabilities_of
from .source_failed import SourceFailed

# Whole-string similarity at which a name is a typo away from the query.
CLOSE_RATIO = 85.0

_TIER_ORDER = {tier: index for index, tier in enumerate(MatchTier)}

# At equal relevance the less granular entity leads, so an artist is never
# sorted under its own tracks.
_GRANULARITY = {
    EntityType.ARTIST: 0,
    EntityType.ALBUM: 1,
    EntityType.PLAYLIST: 2,
    EntityType.TRACK: 3,
}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")

_EDITION_WORDS = (
    r"remaster\w*|deluxe|edition|expanded|anniversary|bonus|reissue|mono|stereo"
    r"|explicit|clean|single|version"
)
_BRACKETED_EDITION_RE = re.compile(
    rf"\s*[\(\[][^\)\]]*\b(?:{_EDITION_WORDS})\b[^\)\]]*[\)\]]", re.I
)
_DASHED_EDITION_RE = re.compile(
    rf"\s+[-–—]\s+[^-–—]*\b(?:{_EDITION_WORDS})\b[^-–—]*$", re.I
)

_CANDIDATE_TYPES = (
    SearchType.track,
    SearchType.album,
    SearchType.artist,
    SearchType.playlist,
)


def normalize(text: str) -> str:
    """The form two names are compared in: diacritics, case, punctuation and
    spacing set aside."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_TOKEN_RE.findall(plain.casefold()))


def equivalent_form(text: str) -> str:
    """[normalize] after the corrections a listener would not call a
    difference: a leading article, an edition suffix, ``&`` for ``and``."""
    text = _BRACKETED_EDITION_RE.sub("", text)
    text = _DASHED_EDITION_RE.sub("", text)
    return _ARTICLE_RE.sub("", normalize(text.replace("&", " and ")))


def similarity(query: str, text: str) -> float:
    """Whole-string similarity, 0..100, with a penalty for words and length
    the query did not ask for — the ordering within a tier."""
    q, t = normalize(query), normalize(text)
    if not q or not t:
        return 0.0
    q_tokens, t_tokens = set(q.split()), set(t.split())
    jaccard = len(q_tokens & t_tokens) / len(q_tokens | t_tokens)
    length = min(len(q), len(t)) / max(len(q), len(t))
    return 0.7 * fuzz.ratio(q, t) + 20.0 * jaccard + 10.0 * length


def name_tier(query: str, name: str) -> Optional[MatchTier]:
    """The tier ``name`` alone earns against ``query``; None when it earns
    none of the name tiers."""
    q, n = normalize(query), normalize(name)
    if not q or not n:
        return None
    if q == n:
        return MatchTier.EXACT
    if equivalent_form(query) == equivalent_form(name):
        return MatchTier.EQUIVALENT
    if fuzz.ratio(q, n) >= CLOSE_RATIO:
        return MatchTier.CLOSE
    if q in n or n in q:
        return MatchTier.PARTIAL
    return None


def _context(item: BrowseItem) -> List[str]:
    """The names an item is found through besides its own: an album's
    artist; a track's performer, album artist and album."""
    names: List[Optional[str]] = []
    if item.track is not None:
        track = item.track
        if track.performer is not None:
            names.append(track.performer.name)
        if track.album is not None:
            if track.album.artist is not None:
                names.append(track.album.artist.name)
            names.append(track.album.title)
    elif item.album is not None and item.album.artist is not None:
        names.append(item.album.artist.name)
    return [name for name in names if name]


def match_of(query: str, item: BrowseItem) -> NameMatch:
    """Where ``item`` stands against ``query``: its tier, and the similarity
    of whichever name earned it."""
    tier = name_tier(query, item.name)
    if tier is not None:
        return NameMatch(tier=tier, score=similarity(query, item.name))

    context = _context(item)
    matched = [name for name in context if name_tier(query, name) is not None]
    if matched:
        score = max(similarity(query, name) for name in matched)
        return NameMatch(tier=MatchTier.CONTEXTUAL, score=score)

    score = max(similarity(query, name) for name in [item.name, *context])
    return NameMatch(tier=MatchTier.WEAK, score=score)


@dataclass(frozen=True)
class SourceHits:
    """One source's answer: each kind's hits in the order it gave them, and
    whether that order is a ranking."""

    kinds: Sequence[Sequence[BrowseItem]]
    ranked: bool


def rank(query: str, sources: Sequence[SourceHits]) -> List[BrowseItem]:
    """Every distinct hit, annotated with its match and relevance, best first.

    Relevance falls with a hit's place among its source's hits of its kind;
    ties go to the less granular kind, then to the earlier source.
    """
    seen = set()
    entries = []
    for source_index, source in enumerate(sources):
        for hits in source.kinds:
            placed = _placed(query, hits, source.ranked, seen)
            for place, (item, match) in enumerate(placed):
                relevance = 1.0 / (1 + place)
                annotated = item.model_copy(
                    update={
                        "match": match.model_copy(update={"relevance": relevance})
                    }
                )
                order = (
                    -relevance,
                    _GRANULARITY.get(item.id.type, len(_GRANULARITY)),
                    source_index,
                )
                entries.append((order, annotated))
    entries.sort(key=lambda entry: entry[0])
    return [item for _, item in entries]


def _placed(
    query: str, hits: Sequence[BrowseItem], ranked: bool, seen: set
) -> List[Tuple[BrowseItem, NameMatch]]:
    """One kind's hits not yet seen, with their matches, in the order of their
    places: the source's own when it ranks them, by name otherwise."""
    placed = []
    for item in hits:
        key = item.id.to_string
        if key in seen:
            continue
        seen.add(key)
        placed.append((item, match_of(query, item)))
    if not ranked:
        placed.sort(key=lambda entry: (_TIER_ORDER[entry[1].tier], -entry[1].score))
    return placed


async def collect_name_matches(
    sources: Sequence[BrowseSource], query: str, cfg: SearchConfig
) -> BrowseItemList:
    """Every hit the given sources return for ``query``, ranked as one list.

    Each source is asked for every entity kind at once. A query that names
    nothing — a mood, a genre, filler — is answered empty without asking:
    there is no name to match, and short titles would score against it by
    coincidence.

    Raises:
        SourceFailed: a source's leg raised; its hits are not silently the
            ones that survived.
    """
    if not query.strip() or not sources or not has_navigational_intent(query):
        return EmptyList(0, 0)

    per_source = await asyncio.gather(
        *(_search_all_kinds(source, query, cfg.candidate_limit) for source in sources)
    )
    ranked = rank(query, per_source)
    return BrowseItemList(offset=0, limit=len(ranked), total=len(ranked), items=ranked)


async def _search_all_kinds(
    source: BrowseSource, query: str, limit: int
) -> SourceHits:
    legs = await asyncio.gather(
        *(source.search(kind, query, 0, limit) for kind in _CANDIDATE_TYPES),
        return_exceptions=True,
    )
    for leg in legs:
        if isinstance(leg, BaseException):
            raise SourceFailed(source.module_name(), leg)
    return SourceHits(
        kinds=[leg.items for leg in legs],
        ranked=ModuleCapability.RANKED_SEARCH.value in capabilities_of(source),
    )


# Descriptor vocabulary (instruments + genres + moods). A query token in here is
# a description, not a name, so it doesn't count as navigational intent (see
# has_navigational_intent): "piano" / "upbeat jazz" describe what to discover
# rather than name a thing to look up. Single tokens, matched per-word.
_DESCRIPTOR_WORDS = frozenset({
    # instruments
    "piano", "guitar", "guitars", "violin", "cello", "drums", "drum", "bass",
    "percussion", "saxophone", "sax", "synthesizer", "synth", "synths", "flute",
    "trumpet", "organ", "harp", "harmonica", "accordion", "banjo", "ukulele",
    "clarinet", "vocals", "vocal", "choir", "strings", "brass", "keyboard",
    "acoustic", "instrumental", "orchestra",
    # genres
    "rock", "jazz", "electronic", "electronica", "ambient", "classical", "rap",
    "hop", "pop", "metal", "folk", "blues", "techno", "house", "funk", "soul",
    "reggae", "country", "punk", "disco", "edm", "dubstep", "trance", "indie",
    "gospel", "latin", "orchestral", "soundtrack", "lofi", "grunge", "opera",
    "synthwave", "ska", "swing", "bluegrass",
    # moods (mirrors the mood vocabulary)
    "happy", "upbeat", "energetic", "joyful", "euphoric", "triumphant", "epic",
    "playful", "exciting", "uplifting", "calm", "peaceful", "serene", "chill",
    "relaxed", "relaxing", "soothing", "mellow", "dreamy", "romantic", "tender",
    "warm", "hopeful", "ethereal", "aggressive", "angry", "tense", "anxious",
    "frantic", "menacing", "dark", "eerie", "chaotic", "intense", "sad",
    "melancholic", "somber", "gloomy", "depressing", "mournful", "lonely",
    "bleak", "nostalgic", "wistful", "bittersweet", "mysterious",
})


# Filler / stop words carried by natural-language queries ("play me something
# for tonight"). With the descriptors above, these are the words that should
# NOT count as a name to look up.
_FILLER_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "into", "my", "me", "i", "you", "your", "we", "us",
    "it", "its", "this", "that", "these", "those", "some", "something",
    "anything", "like", "want", "need", "give", "play", "playing", "song",
    "songs", "music", "track", "tracks", "tune", "tunes", "sound", "sounds",
    "playlist", "vibe", "vibes", "mood", "feeling", "feel", "get", "got", "im",
    "am", "are", "is", "be", "now", "tonight", "today", "day", "night", "time",
    "really", "very", "more", "bit", "little", "kinda", "sorta", "stuff",
})


def has_navigational_intent(query: str) -> bool:
    """True if the query has at least one token that is neither a filler nor a
    descriptor word — i.e. plausibly the name of a thing to look up.

    A pure mood/genre/filler phrase ("something melancholic for tonight") has
    none, so name matching is skipped: there is no name to match, and scoring
    a long phrase against short titles yields coincidental hits. Skipping it
    also spares the search() fan-out for discovery queries — the common case.

    One word alone always counts. A word is filler only in the company of
    others: "night" carries no intent in "something for tonight", but typed
    on its own it is someone looking for a thing called Night, and a list
    named for a time of day would otherwise be unfindable by its own name.
    """
    tokens = set(_TOKEN_RE.findall(query.lower()))
    if len(tokens) == 1:
        return True
    return bool(tokens - _FILLER_WORDS - _DESCRIPTOR_WORDS)
