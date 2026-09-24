"""Tests for the /ai_search/suggestions engine (kalinka_server.suggestions)."""

from datetime import date, datetime

import pytest

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    Catalog,
    EntityId,
    EntityType,
    Genre,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_server.config_model import SearchConfig
from kalinka_server.suggestions import (
    DATA_VERSION,
    SuggestionEngine,
    active_holidays,
    build_candidates,
    daypart_for,
    easter_date,
    _fold,
    _keyword_hit,
)


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def test_easter_dates():
    assert easter_date(2024) == date(2024, 3, 31)
    assert easter_date(2025) == date(2025, 4, 20)
    assert easter_date(2026) == date(2026, 4, 5)


@pytest.mark.parametrize(
    "hour,expected",
    [
        (5, "morning"),
        (10, "morning"),
        (11, "afternoon"),
        (16, "afternoon"),
        (17, "evening"),
        (21, "evening"),
        (22, "night"),
        (23, "night"),
        (0, "night"),
        (4, "night"),
    ],
)
def test_daypart_boundaries(hour, expected):
    assert daypart_for(hour) == expected


def test_holiday_windows():
    assert "christmas" in active_holidays(date(2026, 12, 10))
    assert "christmas" not in active_holidays(date(2026, 11, 30))
    # New year wraps the year end.
    assert "new_year" in active_holidays(date(2026, 12, 28))
    assert "new_year" in active_holidays(date(2026, 1, 1))
    assert "valentine" in active_holidays(date(2026, 2, 10))
    assert "halloween" in active_holidays(date(2026, 10, 28))
    # Easter 2026 is April 5: window is the week before through Easter Monday.
    assert "easter" in active_holidays(date(2026, 3, 29))
    assert "easter" in active_holidays(date(2026, 4, 6))
    assert "easter" not in active_holidays(date(2026, 4, 7))
    # An ordinary summer day has no holiday context.
    assert active_holidays(date(2026, 7, 6)) == []


# ---------------------------------------------------------------------------
# Candidate composition
# ---------------------------------------------------------------------------


def test_candidates_unique_and_tagged():
    cands = build_candidates()
    queries = [c.query for c in cands]
    assert len(queries) == len(set(queries))
    contexts = {ctx for c in cands for ctx in c.contexts}
    assert {"morning", "afternoon", "evening", "night"} <= contexts
    assert "holiday:christmas" in contexts
    # Every candidate carries attestation keywords.
    assert all(c.keywords for c in cands)


def test_candidates_have_no_negation():
    # Negation is CLAP's known failure ("instrumental not rock" retrieves
    # rock) — the composed vocabulary must never contain it.
    for c in build_candidates():
        folded = f" {_fold(c.query)} "
        for bad in (" not ", " no ", " without ", " except "):
            assert bad not in folded, c.query


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------


def test_keyword_word_boundaries():
    assert _keyword_hit(_fold("Pop Anthems Vol. 2"), ("pop",))
    assert not _keyword_hit(_fold("Popular Classics"), ("pop",))
    assert _keyword_hit(_fold("Trip-Hop Essentials"), ("hop",))
    assert not _keyword_hit(_fold("Trip-Hop Essentials"), ("hip hop",))
    assert _keyword_hit(_fold("R&B Classics"), ("r&b",))


def test_keyword_stems():
    assert _keyword_hit(_fold("Christmas Carols"), ("carol*",))
    assert _keyword_hit(_fold("The Haunting"), ("haunt*",))
    assert not _keyword_hit(_fold("Charcoal Nights"), ("carol*",))


# ---------------------------------------------------------------------------
# Attestation + serving against a fake library
# ---------------------------------------------------------------------------


def _track_item(idx: int, genre: str | None, title: str | None = None) -> BrowseItem:
    tid = EntityId(id=f"t{idx}", type=EntityType.TRACK, source="localfiles")
    artist = Artist(
        id=EntityId(id=f"a{idx}", type=EntityType.ARTIST, source="localfiles"),
        name=f"Artist {idx}",
    )
    album = Album(
        id=EntityId(id=f"al{idx}", type=EntityType.ALBUM, source="localfiles"),
        title=f"Album {idx}",
        artist=artist,
        genres=[
            Genre(
                id=EntityId(id=genre, type=EntityType.GENRE, source="localfiles"),
                name=genre,
            )
        ]
        if genre
        else [],
    )
    track = Track(
        id=tid,
        title=title or f"Track {idx}",
        duration=180,
        album=album,
        performer=artist,
    )
    return BrowseItem(
        id=tid, name=track.title, can_browse=False, can_add=True, track=track
    )


def _card(items: list[BrowseItem]) -> BrowseItemList:
    cat = EntityId(id="ai_search:tracks", type=EntityType.CATALOG, source="localfiles")
    card = BrowseItem(
        id=cat,
        name="FROM MY LIBRARY",
        can_browse=False,
        can_add=False,
        catalog=Catalog(
            id=cat,
            title="FROM MY LIBRARY",
            sources=["localfiles"],
            preview_config=Preview(
                type=PreviewType.CARD,
                content_type=PreviewContentType.TRACK,
                items_count=len(items),
            ),
        ),
        sections=items,
    )
    return BrowseItemList(offset=0, limit=10, total=1, items=[card])


class FakeLibrary:
    """Duck-typed library module: jazz-only collection."""

    def __init__(self, genre="Jazz", n=10, with_genre=True):
        self._genre = genre
        self._n = n
        self._with_genre = with_genre

    def module_name(self):
        return "localfiles"

    async def ai_search(self, query, offset=0, limit=50):
        return _card(
            [
                _track_item(i, self._genre if self._with_genre else None)
                for i in range(self._n)
            ]
        )

    async def get_indexer_status(self):
        return {"clap_audio": {"total": 100, "done": 100}}


def _engine(library, tmp_path, seed=1234):
    engine = SuggestionEngine(
        library, SearchConfig(), cache_path=str(tmp_path / "cache.json")
    )
    engine._rng.seed(seed)
    return engine


@pytest.mark.asyncio
async def test_attest_scores_by_genre(tmp_path):
    engine = _engine(FakeLibrary(), tmp_path)
    jazz = next(c for c in engine._candidates if "jazz" in c.query.split())
    metal = next(c for c in engine._candidates if "metal" in c.query)
    jazz_score, n, genre_known = await engine._attest_one(jazz)
    metal_score, _, _ = await engine._attest_one(metal)
    assert n == 10 and genre_known == 10
    assert jazz_score >= 0.8  # all hits, all-distinct artists
    assert metal_score == 0.0  # jazz results can't validate a metal query


@pytest.mark.asyncio
async def test_attest_empty_library_discards_run(tmp_path, monkeypatch):
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)

    class EmptyLibrary(FakeLibrary):
        async def ai_search(self, query, offset=0, limit=50):
            return BrowseItemList(offset=0, limit=10, total=0, items=[])

    engine = _engine(EmptyLibrary(), tmp_path)
    await engine._attest_all("fp")
    assert not engine._attested
    assert not (tmp_path / "cache.json").exists()


@pytest.mark.asyncio
async def test_attest_low_metadata_serves_unvalidated(tmp_path, monkeypatch):
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)
    engine = _engine(FakeLibrary(with_genre=False), tmp_path)
    await engine._attest_all("fp")
    assert engine._attested and engine._low_metadata
    result = engine.suggest(5, now=datetime(2026, 7, 6, 9, 0))
    assert not result.attested
    assert len(result.suggestions) == 5
    assert all(s.experimental for s in result.suggestions)


@pytest.mark.asyncio
async def test_serving_validated_pool(tmp_path, monkeypatch):
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)
    engine = _engine(FakeLibrary(), tmp_path)
    await engine._attest_all("fp")
    assert engine._attested and not engine._low_metadata

    # Ordinary July morning: daypart context only. The jazz-only library
    # validates exactly the 3 morning jazz variants, so count=4 is 3
    # validated + the one experimental slot.
    result = engine.suggest(4, now=datetime(2026, 7, 6, 9, 0))
    assert result.attested
    assert len(result.suggestions) == 4
    experimental = [s for s in result.suggestions if s.experimental]
    validated = [s for s in result.suggestions if not s.experimental]
    assert len(experimental) == 1  # exactly one serendipity slot
    assert experimental[0] is result.suggestions[-1]
    assert validated  # jazz candidates cleared the threshold
    for s in validated:
        assert s.context == "morning"
        assert s.score is not None and s.score >= engine._cfg.suggest_min_score / 100


@pytest.mark.asyncio
async def test_holiday_context_served_in_window(tmp_path, monkeypatch):
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)

    class ChristmasJazzLibrary(FakeLibrary):
        async def ai_search(self, query, offset=0, limit=50):
            titles = ["Jingle Bells", "Silent Night", "White Christmas"]
            return _card(
                [
                    _track_item(i, "Jazz", title=titles[i % len(titles)])
                    for i in range(10)
                ]
            )

    engine = _engine(ChristmasJazzLibrary(), tmp_path)
    await engine._attest_all("fp")

    december = engine.suggest(6, now=datetime(2026, 12, 10, 9, 0))
    assert any(s.context == "holiday:christmas" for s in december.suggestions)

    july = engine.suggest(6, now=datetime(2026, 7, 6, 9, 0))
    assert not any(s.context.startswith("holiday:") for s in july.suggestions)


@pytest.mark.asyncio
async def test_unattested_engine_serves_experimental(tmp_path):
    engine = _engine(None, tmp_path)
    result = engine.suggest(4, now=datetime(2026, 7, 6, 20, 0))
    assert not result.attested
    assert len(result.suggestions) == 4
    assert all(s.experimental for s in result.suggestions)
    assert all(s.context == "evening" for s in result.suggestions)


@pytest.mark.asyncio
async def test_no_library_ignores_stale_cache(tmp_path, monkeypatch):
    # Scores persisted while a library module was enabled must not filter
    # the pool after the module is disabled: a Jamendo-only user would get
    # chips suppressed by a library that is no longer searched.
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)
    enabled = _engine(FakeLibrary(), tmp_path)
    await enabled._attest_all("fp-1")
    assert (tmp_path / "cache.json").exists()

    disabled = _engine(None, tmp_path)
    await disabled.refresh_loop()
    assert not disabled._attested and disabled._scores == {}
    result = disabled.suggest(4, now=datetime(2026, 7, 6, 20, 0))
    assert not result.attested
    assert all(s.experimental for s in result.suggestions)


@pytest.mark.asyncio
async def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)
    engine = _engine(FakeLibrary(), tmp_path)
    await engine._attest_all("fp-1")

    fresh = _engine(FakeLibrary(), tmp_path)
    fresh._load_cache()
    assert fresh._attested
    assert fresh._cached_fp == "fp-1"
    assert fresh._scores == engine._scores


def test_cache_version_mismatch_ignored(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        '{"data_version": %d, "fingerprint": "x", "scores": {"q": 1.0}}'
        % (DATA_VERSION + 1)
    )
    engine = SuggestionEngine(None, SearchConfig(), cache_path=str(path))
    engine._load_cache()
    assert not engine._attested and engine._scores == {}


@pytest.mark.asyncio
async def test_count_one_serves_validated_not_experimental(tmp_path, monkeypatch):
    # The experimental slot must never starve the only slot: count=1 with a
    # validated pool returns a validated pick, and during a holiday window
    # the guaranteed holiday slot still applies.
    monkeypatch.setattr("kalinka_server.suggestions._ATTEST_GAP_S", 0)

    class ChristmasJazzLibrary(FakeLibrary):
        async def ai_search(self, query, offset=0, limit=50):
            titles = ["Jingle Bells", "Silent Night", "White Christmas"]
            return _card(
                [
                    _track_item(i, "Jazz", title=titles[i % len(titles)])
                    for i in range(10)
                ]
            )

    engine = _engine(ChristmasJazzLibrary(), tmp_path)
    await engine._attest_all("fp")

    july = engine.suggest(1, now=datetime(2026, 7, 6, 9, 0))
    assert len(july.suggestions) == 1
    assert not july.suggestions[0].experimental

    december = engine.suggest(1, now=datetime(2026, 12, 10, 9, 0))
    assert len(december.suggestions) == 1
    assert not december.suggestions[0].experimental
    assert december.suggestions[0].context == "holiday:christmas"


def test_count_clamped(tmp_path):
    engine = _engine(None, tmp_path)
    assert len(engine.suggest(0, now=datetime(2026, 7, 6, 9, 0)).suggestions) == 1
    assert len(engine.suggest(500, now=datetime(2026, 7, 6, 9, 0)).suggestions) <= 32
