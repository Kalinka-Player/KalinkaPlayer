"""Context-aware AI-search query suggestions (``/ai_search/suggestions``).

Serves N ready-to-run ``/ai_search`` queries matched to the moment — time of
day (morning coffee / afternoon focus / evening wind-down / night) and the
few public holidays CLAP can actually retrieve (christmas, halloween, …) —
and validated against the user's own library, so a christmas chip never
appears for a user who owns no christmas music.

The design is three stages, and only the first two cost anything:

  * **Candidate universe** — composed offline from piece tables below:
    per-daypart mood words and activity phrases crossed with a genre/instrument
    backbone, plus per-holiday full phrases. The vocabulary is restricted to
    what the retrieval stack is measured to handle (MTG-Jamendo benchmark:
    concrete genre/instrument strong; abstract mood words work only through
    the searcher's valence/arousal leg, so mood words come from that
    vocabulary). Negation — a known CLAP failure ("instrumental not rock"
    retrieves rock) — never appears by construction.
  * **Attestation** — a background job runs every candidate through the
    library module's public ``ai_search()`` and scores whether the results
    actually match the asked-for content: keyword agreement between the
    candidate's genre/holiday tokens and the returned tracks' genre + titles,
    damped by artist diversity. CLAP KNN *distance* is deliberately not used —
    measured to carry no relevance signal (it always returns nearest
    neighbours, relevant or not). Scores persist to the state dir keyed by a
    fingerprint of the library's embedding-done counts, so a restart serves
    from cache and only a changed library re-attests.
  * **Serving** — a pure in-memory lookup: resolve daypart/holidays, filter
    validated candidates by context, weighted-sample ``count`` of them.  One
    slot per response is *experimental*: context-matched but not validated,
    the serendipity outlet (and the only kind served for discovery sources
    like Jamendo, whose catalog matches anything). No model runs here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional

from pydantic import BaseModel

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.inputmodule import InputModule

from .config_model import SearchConfig

logger = logging.getLogger(__name__.split(".")[-1])

# Bump when the piece tables / templates / scoring change: invalidates the
# persisted attestation cache so stale scores for queries that no longer
# exist (or were scored differently) are not served.
DATA_VERSION = 1

# Tracks scored per candidate — the user-visible depth of a suggestion card.
_ATTEST_TOP_K = 10
# Pause between attestation probes: the searcher serializes queries behind
# one lock, so a tight loop would starve a user typing a real search.
_ATTEST_GAP_S = 0.25
# Let startup settle (module subprocesses, first CLAP model load) before the
# first attestation pass hits the searcher.
_STARTUP_DELAY_S = 20.0
# Periodic fingerprint re-check: catches a library that finished embedding
# after startup without requiring a server restart.
_REFRESH_INTERVAL_S = 6 * 3600
# Re-check cadence while the library pipeline (scan / enrichment / embedding)
# is still working — attesting against a half-indexed library would cache
# misleading scores until the next fingerprint change.
_BUSY_RECHECK_S = 60.0
# Below this share of results carrying album genre, keyword scoring can't
# tell "irrelevant results" from "unenriched library" — the run degrades to
# unvalidated serving instead of wrongly filtering everything out.
_MIN_GENRE_COVERAGE = 0.2
# Fraction of top-K results that must match the candidate's keywords for a
# perfect-diversity score of 1.0*ratio; the config threshold cuts on the
# combined score (0-100 scale, see SearchConfig.suggest_min_score).
_RECENT_PENALTY = 0.25  # weight multiplier for queries served recently


# ---------------------------------------------------------------------------
# Piece tables
# ---------------------------------------------------------------------------

# Genre / instrument backbone: (phrase used in composed queries, match tokens
# attested against result genre + titles). Tokens are whole words after
# folding; multi-word tokens match as a phrase.
_GENRES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("jazz", ("jazz", "swing", "bebop")),
    ("blues", ("blues",)),
    ("rock", ("rock",)),
    ("heavy metal", ("metal",)),
    ("hip hop", ("hip hop", "rap")),
    ("electronic music", ("electronic", "electronica", "electro", "techno",
                          "house", "trance", "edm", "dance")),
    ("ambient electronic", ("ambient", "atmospheric", "downtempo", "chillout")),
    ("classical music", ("classical", "orchestra", "orchestral", "symphony",
                         "baroque", "chamber")),
    ("piano music", ("piano",)),
    ("acoustic guitar", ("acoustic", "guitar")),
    ("folk", ("folk", "americana", "singer songwriter")),
    ("country music", ("country",)),
    ("funk", ("funk",)),
    ("soul music", ("soul", "r&b", "rnb", "motown")),
    ("reggae", ("reggae", "dub", "ska")),
    ("latin music", ("latin", "salsa", "bossa", "samba")),
    ("pop music", ("pop",)),
    ("indie rock", ("indie", "alternative")),
)

# Dayparts: (start hour inclusive, end hour exclusive) in local time, mood
# words (drawn from the searcher's valence/arousal vocabulary so the mood leg
# engages, aligned with the daypart's V/A region), activity phrases.
_DAYPARTS: dict[str, dict] = {
    "morning": {
        "hours": (5, 11),
        "moods": ("upbeat", "cheerful", "warm", "energetic"),
        "activities": ("for a slow morning coffee", "for a morning run",
                       "to start the day"),
    },
    "afternoon": {
        "hours": (11, 17),
        "moods": ("smooth", "calm", "mellow"),
        "activities": ("for deep focus", "for studying",
                       "for an afternoon of work"),
    },
    "evening": {
        "hours": (17, 22),
        "moods": ("relaxing", "mellow", "warm", "dreamy"),
        "activities": ("for a cozy evening", "to wind down",
                       "for dinner with friends"),
    },
    "night": {
        "hours": (22, 5),
        "moods": ("dark", "atmospheric", "hypnotic", "energetic"),
        "activities": ("for a late night drive", "for a night out",
                       "to fall asleep to"),
    },
}

# Holidays: date window (inclusive; may wrap the year end) and full phrases
# with their attestation tokens. Only holidays with a *timbral* audio
# signature CLAP retrieves (bells, organ, choir) or a strong title vocabulary
# make this table. Easter's window is computed per year (see _active_holidays).
_HOLIDAYS: dict[str, dict] = {
    "christmas": {
        "window": ((12, 1), (12, 26)),
        "phrases": (
            ("christmas carols with sleigh bells",
             ("christmas", "xmas", "carol*", "jingle", "sleigh", "noel",
              "santa", "silent night", "winter")),
            ("cozy christmas jazz",
             ("christmas", "xmas", "carol*", "jingle", "noel", "santa",
              "winter", "jazz")),
            ("festive winter holiday songs",
             ("christmas", "xmas", "holiday*", "festive", "winter", "noel")),
        ),
    },
    "new_year": {
        "window": ((12, 27), (1, 2)),
        "phrases": (
            ("upbeat party anthems to celebrate",
             ("party", "celebrat*", "dance", "anthem*")),
            ("euphoric dance music for a party",
             ("dance", "party", "club", "electronic", "house")),
        ),
    },
    "valentine": {
        "window": ((2, 7), (2, 14)),
        "phrases": (
            ("romantic love songs",
             ("love*", "romanc*", "romantic", "heart*", "kiss*")),
            ("slow romantic jazz",
             ("love*", "romanc*", "romantic", "heart*", "jazz")),
        ),
    },
    "easter": {
        "window": None,  # computed from the Easter date
        "phrases": (
            ("peaceful choral music",
             ("choral", "choir*", "hymn*", "gospel", "sacred", "chant*")),
            ("gentle acoustic music for a spring morning",
             ("spring", "acoustic", "morning", "gentle")),
        ),
    },
    "halloween": {
        "window": ((10, 24), (10, 31)),
        "phrases": (
            ("spooky eerie halloween music",
             ("halloween", "spooky", "ghost*", "monster*", "witch*",
              "haunt*", "creep*", "zombie*")),
            ("dark haunting organ music",
             ("organ", "haunt*", "dark", "gothic")),
        ),
    },
}


@dataclass
class _Candidate:
    query: str
    contexts: set = field(default_factory=set)  # daypart names / "holiday:x"
    keywords: tuple = ()


def build_candidates() -> List[_Candidate]:
    """Compose the candidate universe from the piece tables.

    Per (daypart, genre) three short variants — "{mood} {genre}",
    "{genre} {activity}", "{mood} {genre} {activity}" — with moods/activities
    rotated deterministically so every piece gets used without a full cross
    product (the universe must stay small enough to attest in ~a minute).
    Duplicate query texts merge their context tags.
    """
    by_query: dict[str, _Candidate] = {}

    def add(query: str, context: str, keywords: tuple) -> None:
        cand = by_query.setdefault(query, _Candidate(query, set(), keywords))
        cand.contexts.add(context)

    for daypart, spec in _DAYPARTS.items():
        moods, acts = spec["moods"], spec["activities"]
        for i, (phrase, keywords) in enumerate(_GENRES):
            mood_a = moods[i % len(moods)]
            mood_b = moods[(i + 1) % len(moods)]
            act_a = acts[i % len(acts)]
            act_b = acts[(i + 1) % len(acts)]
            add(f"{mood_a} {phrase}", daypart, keywords)
            add(f"{phrase} {act_a}", daypart, keywords)
            add(f"{mood_b} {phrase} {act_b}", daypart, keywords)

    for name, spec in _HOLIDAYS.items():
        for query, keywords in spec["phrases"]:
            add(query, f"holiday:{name}", keywords)

    return list(by_query.values())


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def easter_date(year: int) -> date:
    """Western (Gregorian) Easter Sunday — anonymous/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def daypart_for(hour: int) -> str:
    for name, spec in _DAYPARTS.items():
        start, end = spec["hours"]
        if (start <= hour < end) if start < end else (hour >= start or hour < end):
            return name
    return "evening"  # unreachable — the windows cover 0-23


def _in_window(d: date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """Inclusive (month, day) window test; ``start > end`` wraps the year."""
    s, e, v = start, end, (d.month, d.day)
    return (s <= v <= e) if s <= e else (v >= s or v <= e)


def active_holidays(d: date) -> List[str]:
    names: List[str] = []
    for name, spec in _HOLIDAYS.items():
        if name == "easter":
            easter = easter_date(d.year)
            if easter - timedelta(days=7) <= d <= easter + timedelta(days=1):
                names.append(name)
        elif _in_window(d, *spec["window"]):
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class Suggestion(BaseModel):
    query: str
    # Which context produced it: a daypart name or "holiday:<name>".
    context: str
    # True when the suggestion is NOT validated against the library — the
    # per-response serendipity slot, or everything while attestation hasn't
    # produced a usable pool yet.
    experimental: bool = False
    # Attestation score (0-1) when known; absent for experimental picks.
    score: Optional[float] = None


class SuggestionList(BaseModel):
    suggestions: List[Suggestion]
    # True when the non-experimental entries reflect a completed attestation
    # run against the library with usable metadata.
    attested: bool


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Casefold and collapse non-alphanumerics to single spaces, so keyword
    matching is word-boundary safe ("pop" must not hit "popular") and
    punctuation variants agree ("r&b" == "R&B" == "r b")."""
    out = []
    prev_space = True
    for ch in text.casefold():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def _keyword_hit(folded_text: str, keywords: tuple) -> bool:
    padded = f" {folded_text} "
    for kw in keywords:
        # A trailing "*" marks a stem ("carol*" hits "carols", "caroling") —
        # matched as a word prefix. Everything else matches whole words only,
        # so "pop" never hits "popular".
        if kw.endswith("*"):
            fkw = _fold(kw[:-1])
            if fkw and f" {fkw}" in padded:
                return True
        else:
            fkw = _fold(kw)
            if fkw and f" {fkw} " in padded:
                return True
    return False


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SuggestionEngine:
    """Owns the candidate pool, its attestation scores, and serving.

    ``library`` is the module suggestions are validated against — localfiles.
    Discovery sources (Jamendo) match anything, so validating against them
    proves nothing; without a library module the pool serves unvalidated.
    """

    def __init__(
        self,
        library: Callable[[], Optional[InputModule]],
        cfg: SearchConfig,
        cache_path: Optional[str] = None,
    ):
        self._library_now = library
        self._cfg = cfg
        self._cache_path = cache_path or os.path.join(
            paths.state_dir(), "server", "ai_suggestions.json"
        )
        self._candidates = build_candidates()
        self._scores: dict[str, float] = {}
        self._attested = False       # a run completed and was adopted
        self._low_metadata = False   # run completed but genre coverage too thin
        self._cached_fp: Optional[str] = None
        self._recent: deque = deque(maxlen=24)
        self._rng = random.Random()

    @property
    def _library(self) -> Optional[InputModule]:
        """The library as it is now: resolved per use, since the module can
        be switched off, set up again or replaced while the server runs."""
        return self._library_now()

    # -- lifecycle ---------------------------------------------------------

    async def refresh_loop(self) -> None:
        """Load the persisted scores, then (re-)attest whenever the library
        fingerprint moves. Runs for the server's lifetime; cancelled on
        shutdown."""
        if self._library is None:
            # Deliberately skip the cache too: scores persisted while a
            # library module was enabled must not filter the pool after it
            # is disabled — everything serves unvalidated, context-only.
            logger.info("suggestions: no library module — serving unvalidated")
            while self._library is None:
                await asyncio.sleep(_BUSY_RECHECK_S)
        self._load_cache()
        await asyncio.sleep(_STARTUP_DELAY_S)
        while True:
            try:
                if self._library is None:
                    await asyncio.sleep(_BUSY_RECHECK_S)
                    continue
                if await self._pipeline_busy():
                    logger.debug(
                        "suggestions: library pipeline busy — deferring attestation"
                    )
                    await asyncio.sleep(_BUSY_RECHECK_S)
                    continue
                fp = await self._fingerprint()
                if fp != self._cached_fp:
                    await self._attest_all(fp)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("suggestions: attestation pass failed")
            await asyncio.sleep(_REFRESH_INTERVAL_S)

    async def _indexer_status(self) -> dict:
        """The library's per-stage pipeline status; {} when the module has
        no get_indexer_status or the call fails."""
        status_fn = getattr(self._library, "get_indexer_status", None)
        if status_fn is None:
            return {}
        try:
            return await status_fn()
        except Exception as e:
            logger.debug("suggestions: indexer status unavailable: %s", e)
            return {}

    async def _pipeline_busy(self) -> bool:
        """True while the library still has indexing / enrichment / embedding
        work outstanding. Attestation waits it out: probing a half-indexed
        library caches misleading scores."""
        status = await self._indexer_status()
        return any(
            v.get("pending", 0) > 0 or v.get("in_progress", 0) > 0
            for v in status.values()
        )

    async def _fingerprint(self) -> str:
        """Identity of the attestation inputs: piece-table version + the
        library's per-stage **done** counts (stable once the pipeline
        settles, unlike pending/in-progress which move during a run). The
        ``indexing`` stage is excluded — it only exists while a scan runs,
        and hashing it would make the fingerprint flap across scans of an
        unchanged library."""
        status = await self._indexer_status()
        done = {
            k: v.get("done", 0) for k, v in status.items() if k != "indexing"
        }
        raw = json.dumps({"v": DATA_VERSION, "done": done}, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    # -- attestation -------------------------------------------------------

    async def _attest_all(self, fingerprint: str) -> None:
        logger.info("suggestions: attesting %d candidates against %s",
                    len(self._candidates), self._library.module_name())
        scores: dict[str, float] = {}
        genre_seen = 0
        tracks_seen = 0
        for cand in self._candidates:
            score, n_tracks, n_genre = await self._attest_one(cand)
            scores[cand.query] = score
            tracks_seen += n_tracks
            genre_seen += n_genre
            await asyncio.sleep(_ATTEST_GAP_S)

        if tracks_seen == 0:
            # Library empty or search stack not up yet: nothing was actually
            # tested. Keep whatever pool we had; the next fingerprint change
            # (embedding progressing) retries.
            logger.info("suggestions: library returned no tracks — run discarded")
            return

        self._scores = scores
        self._low_metadata = (genre_seen / tracks_seen) < _MIN_GENRE_COVERAGE
        self._attested = True
        self._cached_fp = fingerprint
        self._save_cache()
        thr = self._cfg.suggest_min_score / 100.0
        logger.info(
            "suggestions: attested — %d/%d candidates validated (genre "
            "coverage %.0f%%%s)",
            sum(1 for s in scores.values() if s >= thr), len(scores),
            100.0 * genre_seen / tracks_seen,
            ", too thin — serving unvalidated" if self._low_metadata else "",
        )

    async def _attest_one(self, cand: _Candidate) -> tuple[float, int, int]:
        """Score one candidate: (score 0-1, tracks seen, tracks with genre).

        Keyword agreement over the top-K returned tracks' genre + album/track
        titles + artist name, damped by artist diversity so ten cuts of one
        album can't validate a query.
        """
        try:
            result = await self._library.ai_search(cand.query, 0, _ATTEST_TOP_K)
        except Exception as e:
            logger.warning("suggestions: probe %r failed: %s", cand.query, e)
            return 0.0, 0, 0
        tracks = [
            item.track
            for card in result.items
            for item in (card.sections or [])
            if item.track is not None
        ][:_ATTEST_TOP_K]
        if not tracks:
            return 0.0, 0, 0

        hits = 0
        genre_known = 0
        artists = set()
        for t in tracks:
            parts = [t.title]
            if t.album is not None:
                parts.append(t.album.title)
                if t.album.genres:
                    genre_known += 1
                    parts.extend(genre.name for genre in t.album.genres)
            if t.performer is not None:
                parts.append(t.performer.name)
                artists.add(t.performer.id.to_string)
            if _keyword_hit(_fold(" ".join(p for p in parts if p)), cand.keywords):
                hits += 1
        diversity = len(artists) / len(tracks) if artists else 0.0
        score = (hits / len(tracks)) * (0.5 + 0.5 * diversity)
        return score, len(tracks), genre_known

    # -- persistence -------------------------------------------------------

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("suggestions: unreadable cache %s: %s",
                           self._cache_path, e)
            return
        if data.get("data_version") != DATA_VERSION:
            logger.info("suggestions: cache is v%s, need v%d — re-attesting",
                        data.get("data_version"), DATA_VERSION)
            return
        self._scores = {str(k): float(v) for k, v in data.get("scores", {}).items()}
        self._low_metadata = bool(data.get("low_metadata", False))
        self._cached_fp = data.get("fingerprint")
        self._attested = bool(self._scores)
        logger.info("suggestions: loaded %d cached scores", len(self._scores))

    def _save_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            tmp = self._cache_path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "data_version": DATA_VERSION,
                    "fingerprint": self._cached_fp,
                    "low_metadata": self._low_metadata,
                    "scores": self._scores,
                }, f)
            os.replace(tmp, self._cache_path)
        except OSError as e:
            logger.warning("suggestions: cache write failed: %s", e)

    # -- serving -----------------------------------------------------------

    def suggest(self, count: int, now: Optional[datetime] = None) -> SuggestionList:
        """Pick ``count`` context-matched suggestions — validated ones
        weighted by score (recently served ones damped), plus one
        experimental slot. Pure in-memory work, safe on the request path."""
        now = now if now is not None else datetime.now().astimezone()
        count = max(1, min(count, 32))
        contexts = [daypart_for(now.hour)] + [
            f"holiday:{h}" for h in active_holidays(now.date())
        ]

        usable = self._attested and not self._low_metadata
        thr = self._cfg.suggest_min_score / 100.0
        validated: List[tuple[_Candidate, str, float]] = []
        unvalidated: List[tuple[_Candidate, str]] = []
        for cand in self._candidates:
            ctx = next((c for c in contexts if c in cand.contexts), None)
            if ctx is None:
                continue
            score = self._scores.get(cand.query)
            if usable and score is not None and score >= thr:
                validated.append((cand, ctx, score))
            else:
                unvalidated.append((cand, ctx))

        picks: List[Suggestion] = []

        # One slot stays reserved for the experimental pick when there is
        # anything unvalidated to gamble on — but never at the expense of the
        # only slot: count=1 serves a validated suggestion when one exists.
        reserve_experimental = bool(unvalidated) and count > 1
        reserved = count - 1 if reserve_experimental else count

        # An active holiday that survived validation always gets one slot —
        # it's the whole point of the date awareness, and score-weighted
        # sampling alone can bury two christmas chips under sixty dayparts.
        holiday_validated = [v for v in validated if v[1].startswith("holiday:")]
        if holiday_validated and len(picks) < reserved:
            picks.append(self._pick_validated(holiday_validated, validated))

        while validated and len(picks) < reserved:
            picks.append(self._pick_validated(validated, validated))

        # The experimental slot: context-matched, deliberately unvalidated.
        # Prefer an active holiday's phrasing — the most interesting gamble —
        # and let it fill remaining space when validation came up short.
        self._rng.shuffle(unvalidated)
        unvalidated.sort(key=lambda cv: not cv[1].startswith("holiday:"))
        for cand, ctx in unvalidated:
            if len(picks) >= count:
                break
            if any(p.query == cand.query for p in picks):
                continue
            picks.append(Suggestion(query=cand.query, context=ctx,
                                    experimental=True))

        self._recent.extend(p.query for p in picks)
        return SuggestionList(suggestions=picks, attested=usable)

    def _pick_validated(
        self,
        pool: List[tuple[_Candidate, str, float]],
        validated: List[tuple[_Candidate, str, float]],
    ) -> Suggestion:
        """Weighted-sample one entry from ``pool`` and remove it from
        ``validated`` (pool is a subset of / alias for validated)."""
        weights = [
            score * (_RECENT_PENALTY if cand.query in self._recent else 1.0)
            for cand, _, score in pool
        ]
        total = sum(weights)
        pick = pool[-1]
        if total > 0:
            r = self._rng.uniform(0, total)
            for entry, w in zip(pool, weights):
                r -= w
                if r <= 0:
                    pick = entry
                    break
        validated.remove(pick)
        if pool is not validated and pick in pool:
            pool.remove(pick)
        cand, ctx, score = pick
        return Suggestion(query=cand.query, context=ctx, score=round(score, 3))
