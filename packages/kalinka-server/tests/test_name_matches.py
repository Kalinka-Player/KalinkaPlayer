"""Name matches: every hit a source returns, in the source's own order when it
ranks its hits and by name otherwise, merged by reciprocal rank and never cut."""

import pytest

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    EntityId,
    EntityType,
    MatchTier,
    Owner,
    Playlist,
    Track,
)
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    ModuleCapability,
    SearchType,
)

from kalinka_server.config_model import SearchConfig
from kalinka_server.source_failed import SourceFailed
from kalinka_server.name_matches import (
    SourceHits,
    collect_name_matches,
    equivalent_form,
    has_navigational_intent,
    name_tier,
    normalize,
    rank,
    similarity,
)


def _artist(source, local, name):
    aid = EntityId(id=local, type=EntityType.ARTIST, source=source)
    return BrowseItem(
        id=aid, name=name, can_browse=True, artist=Artist(id=aid, name=name)
    )


def _album(source, local, title, artist_name="Someone"):
    alid = EntityId(id=local, type=EntityType.ALBUM, source=source)
    arid = EntityId(id=f"ar-{local}", type=EntityType.ARTIST, source=source)
    return BrowseItem(
        id=alid,
        name=title,
        can_browse=True,
        album=Album(id=alid, title=title, artist=Artist(id=arid, name=artist_name)),
    )


def _playlist(source, local, name):
    pid = EntityId(id=local, type=EntityType.PLAYLIST, source=source)
    oid = EntityId(id=f"o-{local}", type=EntityType.ARTIST, source=source)
    return BrowseItem(
        id=pid,
        name=name,
        can_browse=True,
        playlist=Playlist(
            id=pid,
            name=name,
            owner=Owner(name="You", id=oid),
            description=None,
            track_count=1,
        ),
    )


def _track(source, local, title, performer="Someone", album_title="An Album"):
    tid = EntityId(id=local, type=EntityType.TRACK, source=source)
    alid = EntityId(id=f"al-{local}", type=EntityType.ALBUM, source=source)
    arid = EntityId(id=f"ar-{local}", type=EntityType.ARTIST, source=source)
    return BrowseItem(
        id=tid,
        name=title,
        can_add=True,
        track=Track(
            id=tid,
            title=title,
            duration=1,
            album=Album(id=alid, title=album_title),
            performer=Artist(id=arid, name=performer),
        ),
    )


def _tiers(items):
    return [item.match.tier for item in items]


def _ids(items):
    return [item.id.id for item in items]


def _hits(items, ranked=False):
    """One source's hits, each kind in the order given."""
    kinds = {}
    for item in items:
        kinds.setdefault(item.id.type, []).append(item)
    return SourceHits(kinds=list(kinds.values()), ranked=ranked)


def _rank(query, items, ranked=False):
    return rank(query, [_hits(items, ranked)])


class TestNormalisation:
    def test_case_diacritics_punctuation_and_spacing_are_set_aside(self):
        assert normalize("  Jean-Michel  JARRE ") == "jean michel jarre"
        assert normalize("Női Kabát") == "noi kabat"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("The Beatles", "beatles"),
            ("Abbey Road (2009 Remaster)", "abbey road"),
            ("Abbey Road - Deluxe Edition", "abbey road"),
            ("Help! [Bonus Tracks]", "help"),
            ("Simon & Garfunkel", "simon and garfunkel"),
        ],
    )
    def test_equivalent_form_sets_aside_what_a_listener_would_not_call_a_difference(
        self, raw, expected
    ):
        assert equivalent_form(raw) == expected

    def test_a_remix_is_not_an_edition(self):
        assert equivalent_form("Yesterday (Radio Remix)") == "yesterday radio remix"


class TestTiers:
    @pytest.mark.parametrize(
        "query,name,tier",
        [
            ("the beatles", "The Beatles", MatchTier.EXACT),
            ("noi kabat", "Női Kabát", MatchTier.EXACT),
            ("beatles", "The Beatles", MatchTier.EQUIVALENT),
            ("abbey road", "Abbey Road (2009 Remaster)", MatchTier.EQUIVALENT),
            ("the beatels", "The Beatles", MatchTier.CLOSE),
            ("beat", "The Beatles", MatchTier.PARTIAL),
            ("the beatles", "The Beatles 1962–1966", MatchTier.PARTIAL),
            ("the beatles white album", "The Beatles", MatchTier.PARTIAL),
        ],
    )
    def test_name_alone(self, query, name, tier):
        assert name_tier(query, name) is tier

    def test_a_name_that_explains_nothing_earns_no_name_tier(self):
        assert name_tier("the beatles", "Come Together") is None
        assert name_tier("", "The Beatles") is None

    def test_context_places_a_hit_below_every_name_tier(self):
        ranked = _rank(
            "the beatles",
            [
                _track("q", "t1", "Come Together", performer="The Beatles"),
                _album("q", "a1", "Revolver", artist_name="The Beatles"),
                _track("q", "t2", "Something", album_title="The Beatles"),
                _track("q", "t3", "Yellow", performer="Coldplay"),
            ],
        )
        assert _tiers(ranked) == [
            MatchTier.CONTEXTUAL,
            MatchTier.CONTEXTUAL,
            MatchTier.CONTEXTUAL,
            MatchTier.WEAK,
        ]


class TestOrdering:
    def test_an_unranked_source_orders_each_kind_by_tier_then_score(self):
        ranked = _rank(
            "the beatles",
            [
                _album("q", "long", "The Beatles 1962–1966"),
                _track("q", "ctx", "Come Together", performer="The Beatles"),
                _artist("q", "typo", "The Beatels"),
                _album("q", "exact", "The Beatles"),
                _artist("q", "art", "Beatles"),
            ],
        )
        assert _ids(ranked) == ["art", "exact", "ctx", "typo", "long"]
        assert _tiers(ranked) == [
            MatchTier.EQUIVALENT,
            MatchTier.EXACT,
            MatchTier.CONTEXTUAL,
            MatchTier.CLOSE,
            MatchTier.PARTIAL,
        ]

    def test_a_ranked_source_keeps_its_own_order_within_each_kind(self):
        ranked = _rank(
            "pink floyd",
            [
                _artist("tidal", "band", "Pink Floyd"),
                _artist("tidal", "gilmour", "David Gilmour"),
                _artist("tidal", "empty", "Pink Floyd"),
            ],
            ranked=True,
        )
        assert _ids(ranked) == ["band", "gilmour", "empty"]
        assert _tiers(ranked) == [MatchTier.EXACT, MatchTier.WEAK, MatchTier.EXACT]

    def test_kinds_take_turns_by_place(self):
        ranked = _rank(
            "pink floyd",
            [
                _artist("tidal", "a0", "Pink Floyd"),
                _artist("tidal", "a1", "David Gilmour"),
                _album("tidal", "b0", "The Wall", artist_name="Pink Floyd"),
                _album("tidal", "b1", "Animals", artist_name="Pink Floyd"),
                _track("tidal", "t0", "Time", performer="Pink Floyd"),
            ],
            ranked=True,
        )
        assert _ids(ranked) == ["a0", "b0", "t0", "a1", "b1"]

    def test_sources_take_turns_by_place(self):
        ranked = rank(
            "pink floyd",
            [
                _hits(
                    [
                        _artist("localfiles", "l0", "Pink Floyd"),
                        _artist("localfiles", "l1", "Pink Floyd Tribute"),
                    ]
                ),
                _hits(
                    [
                        _artist("tidal", "t0", "Pink Floyd"),
                        _artist("tidal", "t1", "David Gilmour"),
                    ],
                    ranked=True,
                ),
            ],
        )
        assert [(item.id.source, item.id.id) for item in ranked] == [
            ("localfiles", "l0"),
            ("tidal", "t0"),
            ("localfiles", "l1"),
            ("tidal", "t1"),
        ]

    def test_relevance_falls_with_place(self):
        ranked = _rank(
            "help",
            [_track("q", "a", "Help"), _track("q", "b", "Help"), _track("q", "c", "Help")],
        )
        assert [item.match.relevance for item in ranked] == pytest.approx(
            [1.0, 1 / 2, 1 / 3]
        )

    def test_extra_words_rank_a_containing_title_below_the_bare_one(self):
        assert similarity("the beatles", "The Beatles") > similarity(
            "the beatles", "The Beatles 1962–1966"
        )

    def test_at_equal_relevance_the_artist_leads_its_own_track(self):
        ranked = _rank(
            "yesterday",
            [_track("q", "t", "Yesterday"), _artist("q", "a", "Yesterday")],
        )
        assert [item.id.type for item in ranked] == [
            EntityType.ARTIST,
            EntityType.TRACK,
        ]

    def test_at_equal_relevance_the_kind_leads_the_score(self):
        ranked = _rank(
            "come together",
            [
                _track("q", "t", "Come Together (Live)"),
                _album("q", "a", "Come Together: The Very Best Of Everything"),
            ],
        )
        assert _tiers(ranked) == [MatchTier.PARTIAL, MatchTier.PARTIAL]
        assert _ids(ranked) == ["a", "t"]
        assert ranked[0].match.score < ranked[1].match.score

    def test_within_an_unranked_kind_the_closer_name_leads(self):
        ranked = _rank(
            "the beatles",
            [
                _album("q", "bbc", "The Beatles Live at the BBC"),
                _album("q", "red", "The Beatles 1962–1966"),
            ],
        )
        assert _ids(ranked) == ["red", "bbc"]

    def test_a_full_tie_keeps_the_sources_own_order(self):
        ranked = _rank(
            "help",
            [_track("q", "first", "Help"), _track("q", "second", "Help")],
        )
        assert _ids(ranked) == ["first", "second"]

    def test_nothing_is_cut(self):
        ranked = _rank("the beatles", [_track("q", "t", "Yellow", performer="Coldplay")])
        assert len(ranked) == 1
        assert ranked[0].match.tier is MatchTier.WEAK

    def test_same_name_from_two_sources_stays_two_rows(self):
        ranked = rank(
            "the beatles",
            [
                _hits([_artist("localfiles", "1", "The Beatles")]),
                _hits([_artist("qobuz", "1", "The Beatles")]),
            ],
        )
        assert [item.id.source for item in ranked] == ["localfiles", "qobuz"]

    def test_an_id_returned_twice_is_one_row(self):
        item = _artist("q", "1", "The Beatles")
        assert len(_rank("the beatles", [item, item])) == 1

    def test_every_hit_carries_its_match(self):
        ranked = _rank("help", [_track("q", "t", "Help"), _album("q", "a", "Let It Be")])
        assert all(item.match is not None for item in ranked)
        assert all(item.match.relevance == 1.0 for item in ranked)
        assert 0 <= ranked[-1].match.score <= 100


class TestIntent:
    def test_names_have_intent(self):
        assert has_navigational_intent("the beatles")
        assert has_navigational_intent("Жанна Агузарова")

    def test_pure_filler_or_descriptor_has_none(self):
        assert not has_navigational_intent("something melancholic for tonight")
        assert not has_navigational_intent("upbeat jazz")
        assert not has_navigational_intent("night time")

    def test_a_lone_word_is_a_name_to_try(self):
        assert has_navigational_intent("night")
        assert has_navigational_intent("chill")
        assert not has_navigational_intent("")


class _Module(InputModule):
    def __init__(self, name, hits=None, failing=None, ranked=False):
        self._name = name
        self._hits = hits or {}
        self._failing = failing or set()
        self._ranked = ranked
        self.asked = []

    def module_name(self):
        return self._name

    def capabilities(self):
        return frozenset({ModuleCapability.RANKED_SEARCH} if self._ranked else ())

    async def search(self, type, query, offset=0, limit=50):
        self.asked.append((type, limit))
        if type in self._failing:
            raise RuntimeError("upstream down")
        items = self._hits.get(type, [])
        return BrowseItemList(offset=offset, limit=limit, total=len(items), items=items)


class TestCollection:
    @pytest.mark.asyncio
    async def test_every_kind_of_every_source_is_asked_and_ranked_as_one(self):
        local = _Module(
            "localfiles",
            {
                SearchType.artist: [_artist("localfiles", "a", "The Beatles")],
                SearchType.track: [
                    _track("localfiles", "t", "Help", performer="The Beatles")
                ],
            },
        )
        qobuz = _Module(
            "qobuz", {SearchType.album: [_album("qobuz", "al", "The Beatles")]}
        )

        result = await collect_name_matches(
            [local, qobuz], "the beatles", SearchConfig()
        )

        assert result.total == 3 == len(result.items)
        assert _tiers(result.items) == [
            MatchTier.EXACT,
            MatchTier.EXACT,
            MatchTier.CONTEXTUAL,
        ]
        assert {kind for kind, _ in local.asked} == set(SearchType)
        assert all(limit == SearchConfig().candidate_limit for _, limit in local.asked)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ranked,order", [(True, ["gilmour", "band"]), (False, ["band", "gilmour"])]
    )
    async def test_only_a_source_declaring_ranked_search_keeps_its_order(
        self, ranked, order
    ):
        module = _Module(
            "tidal",
            {
                SearchType.artist: [
                    _artist("tidal", "gilmour", "David Gilmour"),
                    _artist("tidal", "band", "Pink Floyd"),
                ]
            },
            ranked=ranked,
        )

        result = await collect_name_matches([module], "pink floyd", SearchConfig())

        assert _ids(result.items) == order

    @pytest.mark.asyncio
    async def test_a_query_that_names_nothing_asks_nothing(self):
        module = _Module(
            "qobuz", {SearchType.track: [_track("qobuz", "t", "Something")]}
        )

        result = await collect_name_matches(
            [module], "something melancholic", SearchConfig()
        )

        assert result.total == 0
        assert module.asked == []

    @pytest.mark.asyncio
    async def test_a_lone_filler_word_still_asks(self):
        module = _Module(
            "collections",
            {SearchType.playlist: [_playlist("collections", "p", "Night Drive")]},
        )

        result = await collect_name_matches([module], "night", SearchConfig())

        assert [item.name for item in result.items] == ["Night Drive"]

    @pytest.mark.asyncio
    async def test_a_failing_leg_fails_the_source_rather_than_thinning_it(self):
        module = _Module(
            "qobuz",
            {SearchType.artist: [_artist("qobuz", "a", "The Beatles")]},
            failing={SearchType.track},
        )

        with pytest.raises(SourceFailed) as failure:
            await collect_name_matches([module], "the beatles", SearchConfig())
        assert failure.value.source == "qobuz"

    @pytest.mark.asyncio
    async def test_blank_query_is_empty(self):
        result = await collect_name_matches([_Module("qobuz")], "   ", SearchConfig())
        assert result.total == 0
