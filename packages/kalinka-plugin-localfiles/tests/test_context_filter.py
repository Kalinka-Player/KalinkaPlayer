"""What a track is apart from its sound: languages as tags name them, origins
and decades a query names as facets of the library, holiday music, and the
rules a queue's seeds set for what carries it on."""

import asyncio
import sqlite3

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.searcher.context_filter import (
    FromDecades,
    FromOrigins,
    InLanguages,
    NotHoliday,
    TrackContext,
    admitted,
    facets_of,
    seed_rules,
)
from kalinka_plugin_localfiles.searcher.languages import canonical_language, language_spellings
from kalinka_plugin_localfiles.searcher.query_terms import Origin, origins_in
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb

ITALIAN = Origin("ita", frozenset({"IT"}))
FRENCH = Origin("fra", frozenset({"FR"}))
RUSSIAN = Origin("rus", frozenset({"RU"}))
AMERICAN = Origin(None, frozenset({"US"}))


@pytest.mark.parametrize(
    "raw,code",
    [
        ("eng", "eng"),
        ("English", "eng"),
        ("en", "eng"),
        (" ger ", "deu"),
        ("Deutsch", "deu"),
        ("Русский", "rus"),
        ("nob", "nor"),
        ("FRE", "fra"),
        ("mul", None),
        ("zxx", None),
        ("Klingon", None),
        ("Franc\u0327ais", "fra"),
        ("", None),
        (None, None),
    ],
)
def test_a_tag_names_its_language_in_any_common_form(raw, code):
    assert canonical_language(raw) == code


def test_a_languages_spellings_read_back_as_its_code():
    spellings = language_spellings("rus")
    assert {"rus", "RUS", "ru", "Russian", "русский", "Русский"} <= set(spellings)
    assert {canonical_language(s) for s in spellings} == {"rus"}


@pytest.mark.parametrize(
    "query,origins",
    [
        ("italian 80s", [ITALIAN]),
        ("Russian rock", [RUSSIAN]),
        ("french and italian pop", [FRENCH, ITALIAN]),
        ("italo disco", [ITALIAN]),
        ("american folk", [AMERICAN]),
        ("russian, russian", [RUSSIAN]),
        ("french house", [FRENCH]),
        ("french horn solo", []),
        ("spanish guitar", []),
        ("bach french suites", []),
        ("italian concerto", []),
        ("hungarian dances", []),
        ("sad piano", []),
        ("frenchcore", []),
        ("latin american jazz", []),
        ("South American folk", []),
        ("african american gospel", []),
        ("afro-american soul", []),
        ("west indian calypso", []),
        ("latin american and italian songs", [ITALIAN]),
    ],
)
def test_a_query_names_its_origins(query, origins):
    assert origins_in(query) == origins


def test_a_query_names_its_decades_and_origins_as_facets():
    assert facets_of("italian 80s") == [FromDecades((1980,)), FromOrigins((ITALIAN,))]
    assert facets_of("sad piano") == []


def _library(path, rows):
    """rows: (track_id, album language, album year, artist country)."""
    asyncio.run(init_db(path))
    with sqlite3.connect(path) as conn:
        for track_id, language, year, country in rows:
            conn.execute(
                "INSERT INTO artists (id, name, country, last_updated) VALUES (?, ?, ?, 1)",
                (f"ar-{track_id}", track_id, country),
            )
            conn.execute(
                "INSERT INTO albums (id, title, artist_id, year, language, last_updated)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (f"al-{track_id}", track_id, f"ar-{track_id}", year, language),
            )
            conn.execute(
                "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format, last_updated)"
                " VALUES (?, ?, ?, ?, ?, 'flac', 1)",
                (track_id, track_id, f"al-{track_id}", f"ar-{track_id}", f"/m/{track_id}"),
            )
    return AsyncSearcherDb(LocalFilesConfig(db_path=path))


@pytest.fixture
def library(tmp_path):
    return _library(
        str(tmp_path / "f.db"),
        [
            ("it-80s", "ita", 1984, None),
            ("it-90s", "Italian", 1994, None),
            ("it-artist-en", "eng", 1986, " it"),
            ("gb-80s", "eng", 1983, "GB"),
            ("ru", "Русский", 1988, None),
            ("unknown", None, None, None),
        ],
    )


def _matching(db, query):
    return sorted(asyncio.run(db.tracks_matching(facets_of(query))))


def test_an_origin_matches_its_language_in_any_spelling_or_its_artists_country(library):
    assert _matching(library, "italian pop") == ["it-80s", "it-90s", "it-artist-en"]
    assert _matching(library, "russian rock") == ["ru"]
    assert _matching(library, "british rock") == ["gb-80s"]
    assert _matching(library, "japanese pop") == []


def test_a_decade_matches_its_ten_years(library):
    assert _matching(library, "80s") == ["gb-80s", "it-80s", "it-artist-en", "ru"]
    assert _matching(library, "70s and 90s") == ["it-90s"]


def test_every_facet_must_match(library):
    assert _matching(library, "italian 80s") == ["it-80s", "it-artist-en"]


def test_a_library_tells_only_the_facets_it_carries_facts_for(tmp_path):
    undated_unplaced = _library(str(tmp_path / "u.db"), [("t", None, None, None)])
    placed = _library(str(tmp_path / "p.db"), [("t", None, None, "FR")])
    facets = facets_of("italian 80s")

    assert asyncio.run(undated_unplaced.facets_known(facets)) == []
    assert asyncio.run(placed.facets_known(facets)) == [FromOrigins((ITALIAN,))]


def _read(language=None, genre=None, album=None, title=None):
    return TrackContext.read(language, genre, album, title)


def test_a_context_reads_its_language_as_a_code():
    assert _read(language="Russian") == TrackContext(language="rus")
    assert _read(language="mul") == TrackContext()


@pytest.mark.parametrize(
    "fields",
    [
        {"genre": "Holiday"},
        {"genre": "Christmas; Jazz"},
        {"album": "A Christmas Gift for You"},
        {"title": "Last Christmas"},
        {"title": "Jingle Bell Rock"},
        {"album": "Chants de Noël"},
        {"album": "Рождественские песни"},
        {"title": "Новогодняя"},
        {"album": "Chants de Noe\u0308l"},
    ],
)
def test_holiday_music_is_told_by_genre_or_by_a_title_naming_the_season(fields):
    assert _read(**fields).holiday


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "Holiday"},
        {"album": "Lady Day: The Best of Billie Holiday"},
        {"album": "Noel Gallagher's High Flying Birds"},
        {"genre": "Rock"},
        {},
    ],
)
def test_a_title_that_only_says_holiday_is_not_holiday_music(fields):
    assert not _read(**fields).holiday


def test_a_queue_keeps_to_its_seeds_language_and_lets_the_unknown_through():
    rules = seed_rules([TrackContext(language="eng"), TrackContext()])
    candidates = {
        "english": TrackContext(language="eng"),
        "russian": TrackContext(language="rus"),
        "unknown": TrackContext(),
    }
    assert rules == [InLanguages(frozenset({"eng"})), NotHoliday()]
    assert admitted(candidates, rules) == ["english", "unknown"]


def test_seeds_in_two_languages_admit_both():
    rules = seed_rules([TrackContext(language="eng"), TrackContext(language="rus")])
    candidates = {
        "en": TrackContext(language="eng"),
        "ru": TrackContext(language="rus"),
        "fr": TrackContext(language="fra"),
    }
    assert admitted(candidates, rules) == ["en", "ru"]


def test_seeds_of_no_known_language_admit_any():
    assert admitted({"ru": TrackContext(language="rus")}, seed_rules([TrackContext()])) == ["ru"]


def test_holiday_music_follows_only_holiday_music():
    candidates = {"carol": TrackContext(holiday=True), "song": TrackContext()}
    assert admitted(candidates, seed_rules([TrackContext()])) == ["song"]
    assert admitted(candidates, seed_rules([TrackContext(), TrackContext(holiday=True)])) == [
        "carol",
        "song",
    ]


def test_an_origin_is_told_only_by_the_facts_it_reads(tmp_path):
    countries_only = _library(str(tmp_path / "c.db"), [("t", None, None, "FR")])
    languages_only = _library(str(tmp_path / "l.db"), [("t", "fra", None, None)])

    assert asyncio.run(countries_only.facets_known(facets_of("arabic pop"))) == []
    assert asyncio.run(countries_only.facets_known(facets_of("american rock"))) == facets_of("american rock")
    assert asyncio.run(languages_only.facets_known(facets_of("american rock"))) == []
    assert asyncio.run(languages_only.facets_known(facets_of("arabic pop"))) == facets_of("arabic pop")


def test_an_empty_track_language_leaves_its_albums_language(tmp_path):
    db = _library(str(tmp_path / "e.db"), [("t", "ita", None, None)])
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE tracks SET language = ' '")

    assert _matching(db, "italian pop") == ["t"]
    assert asyncio.run(db.track_contexts(["t"])) == {"t": TrackContext(language="ita")}


class _BrokenFacet:
    def matches(self):
        return "no_such_column = ?", [1]

    def known(self):
        return "1"


def test_a_facet_the_library_cannot_be_read_for_lists_nothing_rather_than_no_track(library):
    assert asyncio.run(library.tracks_matching([_BrokenFacet()])) is None
