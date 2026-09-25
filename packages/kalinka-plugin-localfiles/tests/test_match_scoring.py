"""Tests for duration-aware match scoring in the enricher plugins.

Covers:
- The shared duration_bonus / parse_mb_length_seconds helpers.
- MusicBrainz _find_best_match disambiguating same-titled recordings
  when given a target duration.
- AcoustID _get_best_match_info picking the (result, recording) pair
  whose duration matches the local file, not just the first one.
"""

from unittest.mock import MagicMock

import pytest

from kalinka_plugin_localfiles.enricher.match_utils import (
    album_duration_bonus,
    duration_bonus,
    flatten_mb_tracklist,
    parse_mb_length_seconds,
    parse_mb_track_count,
    release_total_length_seconds,
    track_count_bonus,
    tracklist_coverage_bonus,
)
from kalinka_plugin_localfiles.enricher.acoustid_plugin import AcoustIdPlugin
from kalinka_plugin_localfiles.enricher.musicbrainz_plugin import MusicBrainzPlugin


class TestDurationBonus:
    def test_exact_match(self):
        assert duration_bonus(180, 180) == 20.0

    def test_within_two_seconds(self):
        assert duration_bonus(180, 181.5) == 20.0
        assert duration_bonus(180, 178) == 20.0

    def test_within_five_seconds(self):
        assert duration_bonus(180, 184) == 10.0
        assert duration_bonus(180, 176) == 10.0

    def test_neutral_band(self):
        assert duration_bonus(180, 190) == 0.0
        assert duration_bonus(180, 195) == 0.0

    def test_far_off_penalty(self):
        # Live cut, extended mix, etc.
        assert duration_bonus(180, 360) == -50.0
        assert duration_bonus(180, 60) == -50.0

    @pytest.mark.parametrize("target,cand", [(None, 180), (180, None), (0, 180), (180, 0)])
    def test_missing_returns_zero(self, target, cand):
        assert duration_bonus(target, cand) == 0.0

    def test_unparseable_returns_zero(self):
        assert duration_bonus("not-a-number", 180) == 0.0


class TestParseMbLength:
    def test_string_ms(self):
        assert parse_mb_length_seconds("180000") == 180.0

    def test_int_ms(self):
        assert parse_mb_length_seconds(180500) == 180.5

    def test_none(self):
        assert parse_mb_length_seconds(None) is None

    def test_garbage(self):
        assert parse_mb_length_seconds("abc") is None


def _make_mb_plugin():
    """Build a MusicBrainzPlugin without touching network/config."""
    config = MagicMock()
    config.enricher.plugins.musicbrainz.artist_threshold = 70
    config.enricher.plugins.musicbrainz.album_threshold = 70
    config.enricher.plugins.musicbrainz.track_threshold = 70
    config.enricher.plugins.musicbrainz.string_similarity = 0.6
    config.enricher.plugins.musicbrainz.debug_matching = False
    config.enricher.plugins.user_agent = "test/1.0 (test@example.com)"
    config.legacy_tag_encoding = ""
    plugin = MusicBrainzPlugin(config, db_manager=MagicMock())
    # Phase 3 stage-B reads the local tracklist and records candidates; default
    # to "no local tracks", so alignment contributes a 0 bonus and these tests
    # keep exercising the pre-alignment scoring in isolation.
    plugin.db_manager.get_album_tracks_for_alignment = _async_returning([])
    plugin.db_manager.save_release_candidates = _async_returning(None)
    plugin.db_manager.get_accepted_release_candidate = _async_returning(None)
    return plugin


class TestMusicBrainzFindBestMatch:
    def test_duration_picks_correct_recording(self):
        """Two candidates with identical title and MB score, different
        durations — the one matching the local file should win."""
        plugin = _make_mb_plugin()
        # 180s target. Studio version is 180s; live cut is 320s.
        candidates = [
            {"id": "live", "title": "Yesterday", "ext:score": "95", "length": "320000"},
            {"id": "studio", "title": "Yesterday", "ext:score": "95", "length": "180000"},
        ]
        best, score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
            target_duration_s=180,
            length_key="length",
        )
        assert best is not None
        assert best["id"] == "studio"
        assert score == 95

    def test_duration_rescues_lower_title_score(self):
        """A candidate with slightly weaker title but perfect duration
        should beat one with stronger title but wildly wrong duration."""
        plugin = _make_mb_plugin()
        candidates = [
            # ext:score lower, but duration matches exactly
            {"id": "good", "title": "Yesterday", "ext:score": "85", "length": "180000"},
            # ext:score higher but duration is way off (live, 2x length)
            {"id": "bad", "title": "Yesterday", "ext:score": "95", "length": "360000"},
        ]
        best, _score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
            target_duration_s=180,
            length_key="length",
        )
        assert best is not None
        assert best["id"] == "good"

    def test_no_target_duration_preserves_old_behavior(self):
        """When no duration is supplied, scoring reduces to the old
        title+MB-score weighting (higher ext:score wins on tie)."""
        plugin = _make_mb_plugin()
        candidates = [
            {"id": "a", "title": "Yesterday", "ext:score": "80"},
            {"id": "b", "title": "Yesterday", "ext:score": "95"},
        ]
        best, _score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
        )
        assert best is not None
        assert best["id"] == "b"

    def test_alias_rescues_curated_short_form(self):
        """A local tag matching a MusicBrainz alias must pass the
        similarity floor even when the primary name is much longer
        ('Иванушки Int' is a curated alias of 'Иванушки International')."""
        plugin = _make_mb_plugin()
        plugin.string_similarity_threshold = 0.8
        candidates = [
            {
                "id": "ivanushki",
                "name": "Иванушки International",
                "sort-name": "Ivanushki International",
                "ext:score": "100",
                "alias-list": [
                    {"alias": "Ivanushki International", "type": "Artist name"},
                    {"alias": "Иванушки Int."},
                ],
            },
        ]
        best, score, sim = plugin._find_best_match(
            candidates, "Иванушки Int", threshold=70, match_key="name"
        )
        assert best is not None
        assert best["id"] == "ivanushki"
        assert sim == 1.0

    def test_initials_match_passes_the_similarity_floor(self):
        """'В. Цой' names 'Виктор Цой' by initial + surname; MB has no short
        alias for him, so SequenceMatcher alone (≈0.67) would reject the
        score-100 match. The initials rule must carry it — and rank Виктор
        above the other Цойs the search returns."""
        plugin = _make_mb_plugin()
        plugin.string_similarity_threshold = 0.8
        candidates = [
            {
                "id": "viktor",
                "name": "Виктор Цой",
                "sort-name": "Цой, Виктор",
                "ext:score": "100",
                "alias-list": [{"alias": "Victor Tsoi"}, {"alias": "Viktor Tsoy"}],
            },
            {"id": "anita", "name": "Анита Цой", "ext:score": "88"},
            {"id": "kirill", "name": "Кирилл Цой", "ext:score": "81"},
        ]
        best, _score, sim = plugin._find_best_match(
            candidates, "В. Цой", threshold=70, match_key="name"
        )
        assert best is not None
        assert best["id"] == "viktor"
        assert sim >= 0.8

        # The unspaced tag form works too — the initials rule spaces it.
        best, _score, _sim = plugin._find_best_match(
            candidates, "В.Цой", threshold=70, match_key="name"
        )
        assert best is not None and best["id"] == "viktor"

    def test_all_initials_name_gets_no_initials_boost(self):
        """'R. E. M.' carries too little signal for the initials rule; only
        a verbatim/alias match may pass such a name."""
        plugin = _make_mb_plugin()
        assert plugin._initials_similarity("R. E. M.", "Rapid Eye Movement") == 0.0
        assert plugin._initials_similarity("В. Цой", "Виктор Цой") == 0.9
        assert plugin._initials_similarity("В. Цой", "Анита Цой") == 0.0

    def test_dissimilar_name_without_alias_still_rejected(self):
        """The similarity floor still rejects a candidate whose name and
        aliases all differ from the local tag."""
        plugin = _make_mb_plugin()
        plugin.string_similarity_threshold = 0.8
        candidates = [
            {
                "id": "other",
                "name": "Совершенно другая группа",
                "ext:score": "100",
                "alias-list": [{"alias": "Another Band Entirely"}],
            },
        ]
        best, score, _sim = plugin._find_best_match(
            candidates, "Иванушки Int", threshold=70, match_key="name"
        )
        assert best is None
        assert score == 0

    def test_min_margin_rejects_ambiguous_picks(self):
        """When two candidates are within the margin, neither should
        be returned — better to leave the track orphan than commit
        the wrong recording."""
        plugin = _make_mb_plugin()
        candidates = [
            # Two essentially-identical candidates: same title, same MB
            # score, both with perfect duration match. There's no signal
            # to pick between them.
            {"id": "a", "title": "Yesterday", "ext:score": "95", "length": "180000"},
            {"id": "b", "title": "Yesterday", "ext:score": "95", "length": "180000"},
        ]
        best, score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
            target_duration_s=180,
            length_key="length",
            min_margin=5.0,
        )
        assert best is None
        assert score == 0

    def test_min_margin_accepts_clear_winner(self):
        """With a clear margin in either score or duration, the pick
        should still succeed."""
        plugin = _make_mb_plugin()
        candidates = [
            # Clear winner: matches duration; the other has -50 penalty.
            {"id": "winner", "title": "Yesterday", "ext:score": "95", "length": "180000"},
            {"id": "loser", "title": "Yesterday", "ext:score": "95", "length": "320000"},
        ]
        best, _score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
            target_duration_s=180,
            length_key="length",
            min_margin=5.0,
        )
        assert best is not None
        assert best["id"] == "winner"

    def test_min_margin_zero_keeps_old_behavior(self):
        """Default ``min_margin=0`` should not reject anything for
        callers (like artist/album matching) that don't opt in."""
        plugin = _make_mb_plugin()
        candidates = [
            {"id": "a", "title": "Yesterday", "ext:score": "95"},
            {"id": "b", "title": "Yesterday", "ext:score": "95"},
        ]
        best, _score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
        )
        assert best is not None

    def test_missing_length_falls_back_to_neutral(self):
        """A candidate without a length should not be penalized for the
        missing field — it just doesn't get a duration bonus."""
        plugin = _make_mb_plugin()
        candidates = [
            # No length — neutral; ext:score 95.
            {"id": "no_length", "title": "Yesterday", "ext:score": "95"},
            # Wrong duration; ext:score 95 - 50 penalty = ends up lower.
            {"id": "wrong", "title": "Yesterday", "ext:score": "95", "length": "360000"},
        ]
        best, _score, _sim = plugin._find_best_match(
            candidates,
            "Yesterday",
            threshold=70,
            match_key="title",
            target_duration_s=180,
            length_key="length",
        )
        assert best is not None
        assert best["id"] == "no_length"


def _make_acoustid_plugin():
    """Build an AcoustIdPlugin without network."""
    config = MagicMock()
    config.enricher.plugins.acoustid.api_key = "test-key"
    return AcoustIdPlugin(config, db_manager=MagicMock())


class TestAcoustidBestMatch:
    def test_picks_recording_with_matching_duration(self):
        """Top result has two recordings; the one matching duration wins."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    # Same recording metadata appears repeatedly across
                    # AcoustID responses with different release/duration
                    # contexts; we want the one matching the local file.
                    {
                        "id": "rec-live",
                        "title": "Yesterday",
                        "duration": 320,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "rel-live", "title": "Live At Hollywood"}],
                    },
                    {
                        "id": "rec-studio",
                        "title": "Yesterday",
                        "duration": 180,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "rel-help", "title": "Help!"}],
                    },
                ],
            }
        ]
        match = plugin._get_best_match_info(results, target_duration_s=180)
        assert match is not None
        assert match["recording_mbid"] == "rec-studio"
        assert match["album_title"] == "Help!"

    def test_picks_across_results(self):
        """Best score isn't necessarily the right recording when duration
        disagrees; a slightly-lower-score result with right duration
        should be preferred."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.92,
                "recordings": [
                    {
                        "id": "rec-wrong-len",
                        "title": "Yesterday",
                        "duration": 320,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "rel-live", "title": "Live"}],
                    }
                ],
            },
            {
                "score": 0.80,
                "recordings": [
                    {
                        "id": "rec-right-len",
                        "title": "Yesterday",
                        "duration": 180,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "rel-help", "title": "Help!"}],
                    }
                ],
            },
        ]
        # 0.92*100 - 50 = 42  vs  0.80*100 + 20 = 100 → right_len wins.
        match = plugin._get_best_match_info(results, target_duration_s=180)
        assert match is not None
        assert match["recording_mbid"] == "rec-right-len"

    def test_no_target_duration_picks_top_scored(self):
        """Without a target duration, a clearly-higher-scoring result
        still wins."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    {"id": "winner", "title": "X", "duration": 120, "artists": [], "releases": []},
                ],
            },
            {
                "score": 0.75,
                "recordings": [
                    {"id": "loser", "title": "X", "duration": 360, "artists": [], "releases": []},
                ],
            },
        ]
        match = plugin._get_best_match_info(results, target_duration_s=None)
        assert match is not None
        assert match["recording_mbid"] == "winner"

    def test_ambiguous_top_candidates_returns_none(self):
        """Two candidates with effectively-tied combined scores should
        be held as orphan rather than committed to the wrong release."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    {"id": "a", "title": "X", "duration": 180, "artists": [], "releases": [{"id": "rel-a", "title": "A"}]},
                ],
            },
            {
                "score": 0.95,
                "recordings": [
                    {"id": "b", "title": "X", "duration": 180, "artists": [], "releases": [{"id": "rel-b", "title": "B"}]},
                ],
            },
        ]
        # Both produce combined = 95 + 20 = 115. Margin = 0 → ambiguous.
        match = plugin._get_best_match_info(results, target_duration_s=180)
        assert match is None

    def test_skips_recordings_without_mbid(self):
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    {"title": "X", "duration": 180},  # no id
                    {"id": "good", "title": "X", "duration": 180, "artists": [], "releases": []},
                ],
            }
        ]
        match = plugin._get_best_match_info(results, target_duration_s=180)
        assert match is not None
        assert match["recording_mbid"] == "good"

    def test_empty_input(self):
        plugin = _make_acoustid_plugin()
        assert plugin._get_best_match_info([], target_duration_s=180) is None


class TestAcoustidReleasePicking:
    def test_prefers_album_over_compilation(self):
        """When a recording shows up on both the original studio album
        and a later compilation, we should pick the album."""
        plugin = _make_acoustid_plugin()
        recording = {
            "id": "rec",
            "releasegroups": [
                {
                    "type": "Album",
                    "secondarytypes": ["Compilation"],
                    "releases": [{"id": "comp", "title": "Greatest Hits"}],
                },
                {
                    "type": "Album",
                    "secondarytypes": [],
                    "releases": [{"id": "studio", "title": "Help!"}],
                },
            ],
        }
        title, mbid = plugin._pick_best_release(recording)
        assert mbid == "studio"
        assert title == "Help!"

    def test_prefers_ep_over_live_album(self):
        plugin = _make_acoustid_plugin()
        recording = {
            "releasegroups": [
                {
                    "type": "Album",
                    "secondarytypes": ["Live"],
                    "releases": [{"id": "live", "title": "Live at X"}],
                },
                {
                    "type": "EP",
                    "secondarytypes": [],
                    "releases": [{"id": "ep", "title": "The EP"}],
                },
            ],
        }
        _, mbid = plugin._pick_best_release(recording)
        # Album+Live: 30 - 15 = 15; EP: 20. EP wins.
        assert mbid == "ep"

    def test_falls_back_to_flat_releases(self):
        """When no release-group metadata is present, fall back to
        recording.releases (first one)."""
        plugin = _make_acoustid_plugin()
        recording = {
            "releases": [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B"},
            ],
        }
        _, mbid = plugin._pick_best_release(recording)
        assert mbid == "a"

    def test_no_releases_returns_none(self):
        plugin = _make_acoustid_plugin()
        title, mbid = plugin._pick_best_release({"id": "rec"})
        assert title is None and mbid is None

    def test_position_match_lifts_correct_release(self):
        """A release whose tracklist places the recording at the local
        file's track number should beat one whose tracklist doesn't —
        useful for picking the original edition over a bonus-track
        reissue when both share a release-group type."""
        plugin = _make_acoustid_plugin()
        recording = {
            "id": "rec-1",
            "releasegroups": [
                {
                    "type": "Album",
                    "secondarytypes": [],
                    "releases": [
                        {
                            "id": "reissue",
                            "title": "Help! (Deluxe)",
                            "mediums": [
                                {
                                    "position": 1,
                                    "tracks": [
                                        {"id": "rec-1", "position": 8},
                                    ],
                                }
                            ],
                        },
                        {
                            "id": "original",
                            "title": "Help!",
                            "mediums": [
                                {
                                    "position": 1,
                                    "tracks": [
                                        {"id": "rec-1", "position": 5},
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        # Local file claims track 5, disc 1.
        _, mbid = plugin._pick_best_release(recording, track_number=5, disc_number=1)
        assert mbid == "original"

    def test_position_match_score_disc_only(self):
        """Disc match alone (without track match) still contributes."""
        release = {
            "mediums": [
                {"position": 2, "tracks": [{"id": "rec", "position": 3}]},
            ]
        }
        # Track 99 doesn't match, but disc 2 does.
        score = AcoustIdPlugin._position_match_score(
            release, "rec", track_number=99, disc_number=2
        )
        assert score == 10.0

    def test_position_match_score_no_data(self):
        """Missing tracklist or missing local position returns 0."""
        assert AcoustIdPlugin._position_match_score({}, "rec", 1, 1) == 0.0
        assert AcoustIdPlugin._position_match_score(
            {"mediums": [{"tracks": [{"id": "rec", "position": 1}]}]},
            "rec",
            None,
            None,
        ) == 0.0

    def test_position_match_score_recording_not_on_release(self):
        """If the recording isn't found in the tracklist, no bonus."""
        release = {
            "mediums": [
                {"position": 1, "tracks": [{"id": "other-rec", "position": 5}]},
            ]
        }
        assert AcoustIdPlugin._position_match_score(release, "rec", 5, 1) == 0.0

    def test_release_group_score_basics(self):
        # Bare 'Album' is the gold standard.
        assert AcoustIdPlugin._release_group_score("Album", []) == 30
        # Compilation pulls Album back below an EP.
        assert AcoustIdPlugin._release_group_score("Album", ["Compilation"]) == 15
        # Soundtrack penalty is harsher.
        assert AcoustIdPlugin._release_group_score("Album", ["Soundtrack"]) == 5
        # Single beats nothing-known.
        assert AcoustIdPlugin._release_group_score("Single", []) == 15
        # Unknown type → 0.
        assert AcoustIdPlugin._release_group_score(None, None) == 0.0
        assert AcoustIdPlugin._release_group_score("", []) == 0.0


class TestAcoustidLocalAlbumContext:
    """Re-rank using the album's already-known MBID.

    When the local album row has been enriched to a specific MB
    release, the chosen recording's release should match — otherwise
    we end up with a track tagged to a different album than its album
    row says it belongs to.
    """

    def test_recording_has_release_releasegroups(self):
        recording = {
            "releasegroups": [
                {"releases": [{"id": "alpha"}]},
                {"releases": [{"id": "beta"}, {"id": "gamma"}]},
            ]
        }
        assert AcoustIdPlugin._recording_has_release(recording, "alpha") is True
        assert AcoustIdPlugin._recording_has_release(recording, "gamma") is True
        assert AcoustIdPlugin._recording_has_release(recording, "delta") is False

    def test_recording_has_release_flat_fallback(self):
        recording = {"releases": [{"id": "alpha"}, {"id": "beta"}]}
        assert AcoustIdPlugin._recording_has_release(recording, "beta") is True
        assert AcoustIdPlugin._recording_has_release(recording, "x") is False

    def test_pick_best_release_prefers_local_album_match(self):
        """The local album's MBID overrides release-group type
        preference: a compilation that matches the local row wins
        over a standalone studio album that doesn't."""
        plugin = _make_acoustid_plugin()
        recording = {
            "id": "rec",
            "releasegroups": [
                {
                    "type": "Album",
                    "secondarytypes": [],
                    "releases": [{"id": "studio", "title": "Help!"}],
                },
                {
                    "type": "Album",
                    "secondarytypes": ["Compilation"],
                    "releases": [{"id": "comp", "title": "Greatest Hits"}],
                },
            ],
        }
        # Without context: studio wins (Album beats Album+Compilation).
        _, mbid = plugin._pick_best_release(recording)
        assert mbid == "studio"
        # With context = "comp": comp wins despite being a compilation.
        _, mbid = plugin._pick_best_release(recording, local_album_mbid="comp")
        assert mbid == "comp"

    def test_get_best_match_info_prefers_recording_on_local_release(self):
        """When two AcoustID recordings have the same duration and
        score but different release lists, the one whose releases
        include the local album's MBID should win."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    {
                        "id": "rec-other",
                        "title": "Yesterday",
                        "duration": 180,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "compilation", "title": "Hits"}],
                    },
                    {
                        "id": "rec-original",
                        "title": "Yesterday",
                        "duration": 180,
                        "artists": [{"id": "a1", "name": "Beatles"}],
                        "releases": [{"id": "help-album", "title": "Help!"}],
                    },
                ],
            }
        ]
        # Without context: returns the first one (no signal to break the tie).
        match = plugin._get_best_match_info(results, target_duration_s=180)
        # Both candidates score identically, but the ambiguity guard
        # would normally reject. Verify the local-album context lets
        # the right one win:
        match = plugin._get_best_match_info(
            results, target_duration_s=180, local_album_mbid="help-album"
        )
        assert match is not None
        assert match["recording_mbid"] == "rec-original"
        assert match["album_mbid"] == "help-album"

    def test_context_does_not_affect_when_no_match_in_releases(self):
        """A local album MBID with no candidates pointing at it is a
        no-op — duration/score still decides."""
        plugin = _make_acoustid_plugin()
        results = [
            {
                "score": 0.95,
                "recordings": [
                    {
                        "id": "rec",
                        "title": "T",
                        "duration": 180,
                        "artists": [{"id": "a", "name": "A"}],
                        "releases": [{"id": "rel-x", "title": "X"}],
                    }
                ],
            }
        ]
        match = plugin._get_best_match_info(
            results, target_duration_s=180, local_album_mbid="not-present"
        )
        assert match is not None
        assert match["recording_mbid"] == "rec"


# ---------------------------------------------------------------------------
# Album-level scoring helpers (the duration / track-count fix)
# ---------------------------------------------------------------------------


class TestAlbumDurationBonus:
    def test_exact_match(self):
        # Random Access Memories is ~74 minutes.
        assert album_duration_bonus(4440, 4440) == 20.0

    def test_within_fifteen_seconds(self):
        assert album_duration_bonus(4440, 4448) == 20.0
        assert album_duration_bonus(4440, 4425) == 20.0

    def test_within_sixty_seconds(self):
        assert album_duration_bonus(4440, 4480) == 10.0
        assert album_duration_bonus(4440, 4400) == 10.0

    def test_neutral_band(self):
        assert album_duration_bonus(4440, 4540) == 0.0

    def test_far_off_penalty(self):
        # Deluxe edition adds 20 minutes of bonus tracks — clearly different.
        assert album_duration_bonus(4440, 5640) == -50.0

    def test_missing_returns_zero(self):
        assert album_duration_bonus(None, 4440) == 0.0
        assert album_duration_bonus(4440, None) == 0.0
        assert album_duration_bonus(0, 4440) == 0.0


class TestTrackCountBonus:
    def test_exact(self):
        assert track_count_bonus(12, 12) == 15.0

    def test_off_by_one(self):
        assert track_count_bonus(12, 13) == 5.0
        assert track_count_bonus(12, 11) == 5.0

    def test_off_by_two_neutral(self):
        # 12 vs 14 — could legitimately be a bonus-track variant we still
        # want as a fallback match, so 0.0 (not penalized, not rewarded).
        assert track_count_bonus(12, 14) == 0.0

    def test_far_off_penalty(self):
        # 12-track standard vs 16-track deluxe.
        assert track_count_bonus(12, 16) == -20.0

    def test_missing_returns_zero(self):
        assert track_count_bonus(None, 12) == 0.0
        assert track_count_bonus(12, None) == 0.0


class TestParseMbTrackCount:
    def test_medium_track_count_preferred(self):
        # When multiple count fields are present, total-across-media wins.
        rel = {"medium-track-count": "22", "track-count": "11"}
        assert parse_mb_track_count(rel) == 22

    def test_falls_back_to_per_medium_sum(self):
        rel = {
            "medium-list": [
                {"track-count": "11"},
                {"track-count": "11"},
            ]
        }
        assert parse_mb_track_count(rel) == 22

    def test_returns_none_when_unknown(self):
        assert parse_mb_track_count({}) is None
        assert parse_mb_track_count({"medium-list": [{}, {}]}) is None

    def test_zero_count_treated_as_missing(self):
        # MB sometimes returns 0 for a release whose tracklist hasn't
        # been entered. We treat 0 as "no signal" rather than penalize
        # a 12-track local against it.
        assert parse_mb_track_count({"medium-track-count": "0"}) is None
        assert parse_mb_track_count({"track-count": 0}) is None
        assert (
            parse_mb_track_count({"medium-list": [{"track-count": "0"}, {"track-count": "0"}]})
            is None
        )


class TestReleaseTotalLength:
    def test_sums_across_media(self):
        # Two discs, 3 tracks each — lengths in ms.
        release = {
            "medium-list": [
                {
                    "track-list": [
                        {"length": "180000"},
                        {"length": "240000"},
                        {"length": "120000"},
                    ]
                },
                {
                    "track-list": [
                        {"length": "200000"},
                        {"length": "300000"},
                        {"length": "150000"},
                    ]
                },
            ]
        }
        assert release_total_length_seconds(release) == 1190.0

    def test_returns_none_when_no_lengths(self):
        release = {
            "medium-list": [
                {"track-list": [{"length": None}, {"length": None}]},
            ]
        }
        assert release_total_length_seconds(release) is None

    def test_ignores_unparseable_lengths(self):
        release = {
            "medium-list": [
                {"track-list": [{"length": "180000"}, {"length": "garbage"}]},
            ]
        }
        assert release_total_length_seconds(release) == 180.0


# ---------------------------------------------------------------------------
# MusicBrainz enrich_album two-stage scoring
# ---------------------------------------------------------------------------


class TestShortlistReleases:
    def test_track_count_match_promotes_correct_candidate(self):
        """The deluxe edition has a higher MB ext:score (it's more
        notable) but our local has 12 tracks — standard edition's
        track-count match should let it overtake on the shortlist."""
        plugin = _make_mb_plugin()
        candidates = [
            {
                "id": "deluxe",
                "title": "Album",
                "ext:score": "98",
                "medium-track-count": "16",
            },
            {
                "id": "standard",
                "title": "Album",
                "ext:score": "92",
                "medium-track-count": "12",
            },
        ]
        # base score (no count): deluxe = 0.5*98 + 0.5*100 = 99; standard = 0.5*92 + 0.5*100 = 96
        # with bonus: deluxe = 99 + 0 (off by 4) = 79 after -20; standard = 96 + 15 = 111
        shortlist = plugin._shortlist_releases(candidates, "Album", target_track_count=12)
        assert [c[0]["id"] for c in shortlist] == ["standard", "deluxe"]

    def test_falls_back_to_search_score_without_local_count(self):
        plugin = _make_mb_plugin()
        candidates = [
            {"id": "a", "title": "Album", "ext:score": "80"},
            {"id": "b", "title": "Album", "ext:score": "95"},
        ]
        shortlist = plugin._shortlist_releases(candidates, "Album", target_track_count=None)
        assert shortlist[0][0]["id"] == "b"

    def test_threshold_filter(self):
        plugin = _make_mb_plugin()
        candidates = [
            {"id": "low", "title": "Album", "ext:score": "50"},
            {"id": "ok", "title": "Album", "ext:score": "80"},
        ]
        shortlist = plugin._shortlist_releases(candidates, "Album", target_track_count=None)
        assert [c[0]["id"] for c in shortlist] == ["ok"]


class TestEnrichAlbumStageB:
    """End-to-end test of enrich_album with MB mocked.

    Stage B is what makes the difference: stage A might rank the wrong
    candidate first because of MB score / track-count alone, but stage B
    fetches recordings and the right release wins on summed duration.
    """

    @pytest.mark.asyncio
    async def test_picks_release_with_matching_total_duration(self, monkeypatch):
        plugin = _make_mb_plugin()

        # Local: 12-track album, total ~3000s.
        album = {
            "id": "alb1",
            "title": "Album",
            "artist_name": "Artist",
            "track_count": 12,
            "duration": 3000,
        }

        # MB returns three candidates with similar titles; only one has
        # the right total duration. The first two are deluxe-edition
        # variants with 16 tracks (~4200s) — stage A's track-count bonus
        # already filters them out, but we also verify stage B wins.
        search_response = {
            "release-list": [
                {
                    "id": "deluxe1",
                    "title": "Album",
                    "ext:score": "100",
                    "medium-track-count": "16",
                },
                {
                    "id": "deluxe2",
                    "title": "Album",
                    "ext:score": "95",
                    "medium-track-count": "16",
                },
                {
                    "id": "standard",
                    "title": "Album",
                    "ext:score": "90",
                    "medium-track-count": "12",
                },
            ]
        }

        def fake_get_release_by_id(rid, includes=None):
            # Each release returns its full tracklist with lengths.
            release_lengths = {
                "deluxe1": [250000] * 16,    # 4000s — way off our 3000
                "deluxe2": [260000] * 16,    # 4160s — way off
                "standard": [250000] * 12,   # 3000s — exact match
            }
            return {
                "release": {
                    "id": rid,
                    "title": "Album",
                    "date": "2013-05-17",
                    "medium-list": [
                        {
                            "track-list": [
                                {"length": str(ms)} for ms in release_lengths[rid]
                            ],
                        }
                    ],
                    "tag-list": [{"name": "electronic", "count": "5"}],
                }
            }

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_releases",
            lambda *a, **kw: search_response,
        )
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            fake_get_release_by_id,
        )

        result = await plugin.enrich_album(album)
        assert result is not None
        assert result["mbid"] == "standard"
        # genre/year are claims now, not direct updates — resolution is the sole
        # writer (retirement of plugin direct-writes). updates carries only
        # identity + match metadata.
        assert set(result["updates"]) <= {"mbid", "match_score", "match_similarity"}
        claims = {(c["field"], c["value"]) for c in result["claims"]}
        assert ("genre", "electronic") in claims
        assert ("year", 2013) in claims

    @pytest.mark.asyncio
    async def test_holds_orphan_when_top_candidates_indistinguishable(
        self, monkeypatch
    ):
        """Two top candidates in DIFFERENT release-groups with
        identical scores represent genuinely different albums — orphan."""
        plugin = _make_mb_plugin()
        album = {
            "id": "alb1",
            "title": "Album",
            "artist_name": "Artist",
            "track_count": 12,
            "duration": 3000,
        }
        # Two releases with effectively identical signals but DIFFERENT
        # release-groups — i.e. two distinct albums that happen to look
        # alike. Hold as orphan.
        search_response = {
            "release-list": [
                {
                    "id": "a",
                    "title": "Album",
                    "ext:score": "95",
                    "medium-track-count": "12",
                    "release-group": {"id": "rg-1"},
                },
                {
                    "id": "b",
                    "title": "Album",
                    "ext:score": "95",
                    "medium-track-count": "12",
                    "release-group": {"id": "rg-2"},
                },
            ]
        }

        def fake_get(rid, includes=None):
            return {
                "release": {
                    "id": rid,
                    "title": "Album",
                    "medium-list": [
                        {"track-list": [{"length": "250000"} for _ in range(12)]}
                    ],
                }
            }

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_releases",
            lambda *a, **kw: search_response,
        )
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            fake_get,
        )
        result = await plugin.enrich_album(album)
        assert result is None  # within margin → hold as orphan

    @pytest.mark.asyncio
    async def test_commits_when_top_tied_candidates_share_release_group(
        self, monkeypatch
    ):
        """The Abbey Road case: MB has many releases (CD, vinyl,
        remaster, mono mix, …) for the same logical album. Every
        candidate scores 135 and they're all in the SAME release-group.
        The guard should NOT fire — we want to commit one of them
        rather than orphan a clearly-correct match."""
        plugin = _make_mb_plugin()
        album = {
            "id": "alb1",
            "title": "Abbey Road",
            "artist_name": "The Beatles",
            "track_count": 17,
            "duration": 2700,
        }
        # Three candidates from the SAME release-group (every release
        # variant lives under the same group MBID). All score 135.
        search_response = {
            "release-list": [
                {
                    "id": "rel-cd",
                    "title": "Abbey Road",
                    "ext:score": "100",
                    "medium-track-count": "17",
                    "release-group": {"id": "rg-abbey-road"},
                },
                {
                    "id": "rel-vinyl",
                    "title": "Abbey Road",
                    "ext:score": "100",
                    "medium-track-count": "17",
                    "release-group": {"id": "rg-abbey-road"},
                },
                {
                    "id": "rel-remaster",
                    "title": "Abbey Road",
                    "ext:score": "100",
                    "medium-track-count": "17",
                    "release-group": {"id": "rg-abbey-road"},
                },
            ]
        }

        def fake_get(rid, includes=None):
            return {
                "release": {
                    "id": rid,
                    "title": "Abbey Road",
                    "date": "1969-09-26",
                    "medium-list": [
                        {"track-list": [{"length": "159000"} for _ in range(17)]}
                    ],
                }
            }

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_releases",
            lambda *a, **kw: search_response,
        )
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            fake_get,
        )
        result = await plugin.enrich_album(album)
        assert result is not None, "Expected a commit when tied candidates share release-group"
        assert result["mbid"] in {"rel-cd", "rel-vinyl", "rel-remaster"}
        assert ("year", 1969) in {(c["field"], c["value"]) for c in result["claims"]}

    def test_same_release_group_helper(self):
        from kalinka_plugin_localfiles.enricher.musicbrainz_plugin import (
            _same_release_group,
        )
        assert _same_release_group(
            {"release-group": {"id": "rg1"}},
            {"release-group": {"id": "rg1"}},
        )
        # Different groups
        assert not _same_release_group(
            {"release-group": {"id": "rg1"}},
            {"release-group": {"id": "rg2"}},
        )
        # Missing release-group on either side — fall through to the
        # ambiguity guard rather than overconfident "they match" claim.
        assert not _same_release_group(
            {"release-group": {"id": "rg1"}}, {}
        )
        assert not _same_release_group({}, {"release-group": {"id": "rg1"}})
        assert not _same_release_group({}, {})


# ---------------------------------------------------------------------------
# Tracklist position re-keying from the matched MB release
# ---------------------------------------------------------------------------


def _two_disc_release() -> dict:
    """Build an MB release shaped like Pink Floyd's The Wall — two
    media of 13 tracks each, with continuous within-medium numbering.
    The user's vinyl rip will have ``B3`` etc. in titles but
    ``TRACKNUMBER=3`` (within-side), so the matcher has to ignore
    those and place every track at MB's actual canonical position.
    """
    titles_d1 = [
        ("In the Flesh?", 200000),
        ("The Thin Ice", 150000),
        ("Another Brick in the Wall, Part 1", 191000),
        ("The Happiest Days of Our Lives", 111000),
        ("Another Brick in the Wall, Part 2", 241000),
        ("Mother", 334000),
        ("Goodbye Blue Sky", 170000),
        ("Empty Spaces", 127000),
        ("Young Lust", 213000),
        ("One of My Turns", 215000),
        ("Don't Leave Me Now", 257000),
        ("Another Brick in the Wall, Part 3", 78000),
        ("Goodbye Cruel World", 75000),
    ]
    titles_d2 = [
        ("Hey You", 284000),
        ("Is There Anybody Out There?", 163000),
        ("Nobody Home", 207000),
        ("Vera", 96000),
        ("Bring the Boys Back Home", 89000),
        ("Comfortably Numb", 386000),
        ("The Show Must Go On", 98000),
        ("In the Flesh", 260000),
        ("Run Like Hell", 267000),
        ("Waiting for the Worms", 240000),
        ("Stop", 32000),
        ("The Trial", 322000),
        ("Outside the Wall", 104000),
    ]

    def medium(pos, titles):
        return {
            "position": pos,
            "track-list": [
                {
                    "position": i + 1,
                    "length": str(length),
                    "recording": {"id": f"rec-d{pos}-t{i+1}", "title": title},
                }
                for i, (title, length) in enumerate(titles)
            ],
        }

    return {
        "id": "rel-the-wall",
        "title": "The Wall",
        "medium-list": [medium(1, titles_d1), medium(2, titles_d2)],
    }


def _async_returning(value):
    """Build an ``AsyncMock``-style coroutine that returns ``value``,
    so we can wire a fake db_manager method onto a MagicMock-backed
    plugin instance. ``MagicMock`` doesn't natively know how to be
    awaited."""

    async def _coro(*_a, **_kw):
        return value

    return _coro


class TestFlattenMbTracklist:
    def test_flattens_two_disc_release(self):
        flat = flatten_mb_tracklist(_two_disc_release())
        assert len(flat) == 26
        # Disc 1 then disc 2, within-medium positions intact.
        assert (flat[0]["medium"], flat[0]["track"]) == (1, 1)
        assert (flat[12]["medium"], flat[12]["track"]) == (1, 13)
        assert (flat[13]["medium"], flat[13]["track"]) == (2, 1)
        assert (flat[25]["medium"], flat[25]["track"]) == (2, 13)

    def test_ignores_malformed_positions(self):
        release = {
            "medium-list": [
                {"position": "bad", "track-list": [{"position": 1}]},
                {
                    "position": 1,
                    "track-list": [
                        {"position": "x", "recording": {"title": "T"}},
                        {"position": 2, "recording": {"title": "OK"}},
                    ],
                },
            ]
        }
        flat = flatten_mb_tracklist(release)
        assert len(flat) == 1 and flat[0]["title"] == "OK"

    def test_empty_release(self):
        assert flatten_mb_tracklist({}) == []
        assert flatten_mb_tracklist({"medium-list": []}) == []


class TestMusicBrainzInReleaseTrackLookup:
    """Per-track lookup inside a known release. Replaces the old
    batch-match approach with a track-scoped flow: each track's MB
    enrichment computes its own canonical (disc, track) by consulting
    the album's MB release tracklist directly."""

    @pytest.mark.asyncio
    async def test_picks_position_by_title_and_duration(self, monkeypatch):
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_by_id = _async_returning(
            {"id": "a", "title": "The Wall", "mbid": "rel-the-wall"}
        )
        plugin.db_manager.get_artist_by_id = _async_returning(
            {"id": "ar", "name": "Pink Floyd"}
        )

        release = _two_disc_release()
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            lambda *a, **kw: {"release": release},
        )

        # Local "B2 Empty spaces" with tag track_number=2 — sorting by
        # tag track_number alone groups it with all the other "2"
        # tracks. Lookup should put it at disc 1, track 8 (within-
        # medium position on MB's continuous numbering).
        track = {
            "id": "t",
            "title": "B2 Empty spaces",
            "duration": 124,
            "album_id": "a",
            "artist_id": "ar",
            "track_number": 2,
            "disc_number": None,
        }
        result = await plugin.enrich_track(track)
        assert result is not None
        assert result["mbid"] == "rec-d1-t8"
        assert result["updates"]["disc_number"] == 1
        assert result["updates"]["track_number"] == 8

    @pytest.mark.asyncio
    async def test_falls_back_to_global_search_when_album_has_no_mbid(
        self, monkeypatch
    ):
        """Without an album MBID, the in-release path is unreachable —
        the existing global search path takes over."""
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_by_id = _async_returning(
            {"id": "a", "title": "Some Album", "mbid": None}
        )
        plugin.db_manager.get_artist_by_id = _async_returning(
            {"id": "ar", "name": "Some Artist"}
        )

        # Mock the global recording search the fallback hits.
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_recordings",
            lambda *a, **kw: {
                "recording-list": [
                    {
                        "id": "rec-global",
                        "title": "Track",
                        "ext:score": "95",
                        "length": "180000",
                    }
                ]
            },
        )
        track = {
            "id": "t",
            "title": "Track",
            "duration": 180,
            "album_id": "a",
            "artist_id": "ar",
        }
        result = await plugin.enrich_track(track)
        assert result is not None
        assert result["mbid"] == "rec-global"
        # Global search doesn't set disc_number.
        assert "disc_number" not in result["updates"]

    @pytest.mark.asyncio
    async def test_ambiguous_in_release_falls_back_to_global(self, monkeypatch):
        """The Wall has two distinct songs with near-identical titles:
        "In the Flesh?" on disc 1 and "In the Flesh" on disc 2. A local
        track titled simply "In the Flesh" with no duration to
        disambiguate must NOT silently commit to whichever appears
        first in iteration; the runner-up check should reject and let
        the global path try."""
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_by_id = _async_returning(
            {"id": "a", "title": "The Wall", "mbid": "rel-the-wall"}
        )
        plugin.db_manager.get_artist_by_id = _async_returning(
            {"id": "ar", "name": "Pink Floyd"}
        )
        release = _two_disc_release()
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            lambda *a, **kw: {"release": release},
        )
        # Mock the global fallback so we can detect it being hit.
        global_called = {"yes": False}

        def fake_search(*a, **kw):
            global_called["yes"] = True
            return {"recording-list": []}

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_recordings",
            fake_search,
        )
        # No duration → both "In the Flesh?" and "In the Flesh" score
        # close on title similarity alone.
        track = {
            "id": "t",
            "title": "In the Flesh",
            "duration": None,
            "album_id": "a",
            "artist_id": "ar",
        }
        result = await plugin.enrich_track(track)
        # The ambiguity guard fired and Path 2 took over (empty result
        # from the mock = no MB match = None).
        assert result is None
        assert global_called["yes"], (
            "Expected Path 2 (global search) to run after Path 1's "
            "ambiguity guard rejected"
        )

    @pytest.mark.asyncio
    async def test_release_cache_avoids_refetch_per_track(self, monkeypatch):
        """The first track enrichment for an album triggers a single
        ``get_release_by_id`` call; subsequent tracks read from the
        cache. Saves N-1 API calls on a 26-track album."""
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_by_id = _async_returning(
            {"id": "a", "title": "The Wall", "mbid": "rel-the-wall"}
        )
        plugin.db_manager.get_artist_by_id = _async_returning(
            {"id": "ar", "name": "Pink Floyd"}
        )
        release = _two_disc_release()

        fetch_count = {"n": 0}

        def fake_get(rid, includes=None):
            fetch_count["n"] += 1
            return {"release": release}

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            fake_get,
        )
        for title in ("A1 In the flesh", "A2 The thin ice", "B2 Empty spaces"):
            await plugin.enrich_track(
                {
                    "id": title,
                    "title": title,
                    "duration": 180,
                    "album_id": "a",
                    "artist_id": "ar",
                }
            )
        assert fetch_count["n"] == 1

    @pytest.mark.asyncio
    async def test_seeds_cache_on_album_enrichment(self, monkeypatch):
        """When album enrichment fetches the release detail in stage B,
        the same payload is cached. Per-track lookups for that album
        then make zero extra MB calls."""
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_by_id = _async_returning(None)

        release = _two_disc_release()
        search_response = {
            "release-list": [
                {
                    "id": "rel-the-wall",
                    "title": "The Wall",
                    "ext:score": "100",
                    "medium-track-count": "26",
                }
            ]
        }
        fetch_count = {"n": 0}

        def fake_get(rid, includes=None):
            fetch_count["n"] += 1
            return {"release": release}

        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.search_releases",
            lambda *a, **kw: search_response,
        )
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin.musicbrainzngs.get_release_by_id",
            fake_get,
        )

        album = {
            "id": "a",
            "title": "The Wall",
            "artist_name": "Pink Floyd",
            "track_count": 26,
            "duration": 4300,
        }
        result = await plugin.enrich_album(album)
        assert result is not None and result["mbid"] == "rel-the-wall"
        # Stage B fetched once; that should be cached now.
        before = fetch_count["n"]
        assert before == 1
        # The release detail is reachable via the cache without re-call.
        cached = await plugin._get_release_detail("rel-the-wall")
        assert cached is release
        assert fetch_count["n"] == before  # no refetch


class TestStageBTracklistAlignment:
    """Phase 3: stage B aligns the local tracklist against each candidate and
    records what it considered. Alignment reuses the recordings stage B already
    fetches, so it costs no extra MB requests."""

    @staticmethod
    def _search():
        return {"release-list": [
            {"id": "wrong", "title": "Album", "ext:score": "100",
             "medium-track-count": "3"},
            {"id": "right", "title": "Album", "ext:score": "100",
             "medium-track-count": "3"},
        ]}

    @staticmethod
    def _fake_release(rid, includes=None):
        titles = {
            # Same score/track-count; only the tracklist tells them apart.
            "wrong": ["Zzz Unrelated", "Qqq Nothing", "Www Other"],
            "right": ["Alpha Song", "Beta Song", "Gamma Song"],
        }[rid]
        return {"release": {
            "id": rid, "title": "Album",
            "release-group": {"id": f"rg-{rid}"},
            "medium-list": [{"position": "1", "track-list": [
                {"position": str(i + 1), "title": t, "length": "200000",
                 "recording": {"id": f"rec-{rid}-{i}"}}
                for i, t in enumerate(titles)]}],
        }}

    def _plugin_with_local(self, monkeypatch, saved):
        plugin = _make_mb_plugin()
        plugin.db_manager.get_album_tracks_for_alignment = _async_returning([
            {"id": "t0", "title": "Alpha Song", "duration": 200},
            {"id": "t1", "title": "Beta Song", "duration": 200},
            {"id": "t2", "title": "Gamma Song", "duration": 200},
        ])

        async def _save(album_id, provider, rows):
            saved.extend(rows)

        plugin.db_manager.save_release_candidates = _save
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin."
            "musicbrainzngs.search_releases", lambda *a, **k: self._search())
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin."
            "musicbrainzngs.get_release_by_id", self._fake_release)
        return plugin

    @pytest.mark.asyncio
    async def test_alignment_picks_the_matching_tracklist(self, monkeypatch):
        saved = []
        plugin = self._plugin_with_local(monkeypatch, saved)
        album = {"id": "alb1", "title": "Album", "artist_name": "Artist",
                 "track_count": 3, "duration": 600}
        result = await plugin.enrich_album(album)
        assert result is not None
        # Identical ext:score and track count; the tracklist alignment decides.
        assert result["mbid"] == "right"

    @pytest.mark.asyncio
    async def test_candidates_recorded_with_status_and_coverage(self, monkeypatch):
        saved = []
        plugin = self._plugin_with_local(monkeypatch, saved)
        album = {"id": "alb1", "title": "Album", "artist_name": "Artist",
                 "track_count": 3, "duration": 600}
        await plugin.enrich_album(album)
        by_id = {r["release_id"]: r for r in saved}
        assert set(by_id) == {"right", "wrong"}
        # The winner's tracklist lines up -> accepted, with a usable track_map.
        assert by_id["right"]["status"] == "accepted"
        assert by_id["right"]["coverage"] == 1.0
        assert by_id["right"]["track_map"]["t0"][2] == "rec-right-0"
        # The loser is retained for review, never silently dropped.
        assert by_id["wrong"]["status"] == "candidate"
        assert by_id["wrong"]["coverage"] == 0.0

    @pytest.mark.asyncio
    async def test_candidates_recorded_even_when_held_as_orphan(self, monkeypatch):
        """An ambiguous match is held as orphan — but what was considered is
        still recorded, since that is exactly what a human review needs."""
        saved = []
        plugin = self._plugin_with_local(monkeypatch, saved)
        # Both candidates have an unrelated tracklist, so neither gets a
        # coverage bonus: scores tie, release-groups differ -> orphan hold.
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.enricher.musicbrainz_plugin."
            "musicbrainzngs.get_release_by_id",
            lambda rid, includes=None: {"release": {
                "id": rid, "title": "Album",
                "release-group": {"id": f"rg-{rid}"},
                "medium-list": [{"position": "1", "track-list": [
                    {"position": str(i + 1), "title": f"Unrelated {i}",
                     "length": "200000", "recording": {"id": f"rec-{rid}-{i}"}}
                    for i in range(3)]}],
            }})
        album = {"id": "alb1", "title": "Album", "artist_name": "Artist",
                 "track_count": 3, "duration": 600}
        result = await plugin.enrich_album(album)
        assert result is None                      # held as orphan
        assert {r["release_id"] for r in saved} == {"right", "wrong"}
        assert all(r["status"] == "candidate" for r in saved)


class TestAcceptedTrackMapPlacement:
    """Phase 3c-2: an accepted release candidate's track_map places tracks
    directly — globally consistent (the album alignment assigned every slot in
    one pass) and free of both scoring and requests."""

    def _plugin(self, accepted):
        plugin = _make_mb_plugin()
        calls = []

        async def _get(album_id):
            calls.append(album_id)
            return accepted

        plugin.db_manager.get_accepted_release_candidate = _get
        return plugin, calls

    @pytest.mark.asyncio
    async def test_places_track_from_map(self):
        plugin, _ = self._plugin({
            "release_id": "rel-1", "coverage": 1.0,
            "track_map": {"t1": [2, 7, "rec-xyz"]},
        })
        got = await plugin._lookup_track_in_accepted_map({"id": "t1", "album_id": "al1"})
        assert got["mbid"] == "rec-xyz"
        assert got["updates"]["disc_number"] == 2
        assert got["updates"]["track_number"] == 7
        assert got["updates"]["match_score"] == 100

    @pytest.mark.asyncio
    async def test_unmatched_track_falls_through(self):
        plugin, _ = self._plugin({
            "release_id": "rel-1", "coverage": 0.9, "track_map": {"other": [1, 1, "r"]},
        })
        assert await plugin._lookup_track_in_accepted_map(
            {"id": "t1", "album_id": "al1"}) is None

    @pytest.mark.asyncio
    async def test_no_accepted_candidate_falls_through(self):
        plugin, _ = self._plugin(None)
        assert await plugin._lookup_track_in_accepted_map(
            {"id": "t1", "album_id": "al1"}) is None

    @pytest.mark.asyncio
    async def test_unknown_album_is_skipped_without_a_query(self):
        plugin, calls = self._plugin(None)
        assert await plugin._lookup_track_in_accepted_map(
            {"id": "t1", "album_id": "unknown_album"}) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_candidate_is_looked_up_once_per_album(self):
        plugin, calls = self._plugin({
            "release_id": "rel-1", "coverage": 1.0,
            "track_map": {"t1": [1, 1, "r1"], "t2": [1, 2, "r2"]},
        })
        await plugin._lookup_track_in_accepted_map({"id": "t1", "album_id": "al1"})
        await plugin._lookup_track_in_accepted_map({"id": "t2", "album_id": "al1"})
        assert calls == ["al1"]   # cached: N tracks, one query

    @pytest.mark.asyncio
    async def test_db_failure_degrades_to_fallback(self):
        plugin = _make_mb_plugin()

        async def _boom(album_id):
            raise RuntimeError("db down")

        plugin.db_manager.get_accepted_release_candidate = _boom
        assert await plugin._lookup_track_in_accepted_map(
            {"id": "t1", "album_id": "al1"}) is None

    @pytest.mark.asyncio
    async def test_malformed_map_entry_falls_through(self):
        plugin, _ = self._plugin({
            "release_id": "rel-1", "coverage": 1.0,
            "track_map": {"t1": [2], "t2": "garbage"},
        })
        for tid in ("t1", "t2"):
            assert await plugin._lookup_track_in_accepted_map(
                {"id": tid, "album_id": "al1"}) is None


class TestCoverageBonusTrackCountGate:
    """Coverage can't discriminate on a tiny local album: with 1-2 tracks every
    release containing them scores 1.0, so the bonus is withheld."""

    def test_bonus_applies_with_enough_tracks(self):
        assert tracklist_coverage_bonus(1.0, 12) == 20.0
        assert tracklist_coverage_bonus(0.6, 12) == 5.0

    def test_bonus_withheld_on_tiny_albums(self):
        assert tracklist_coverage_bonus(1.0, 1) == 0.0
        assert tracklist_coverage_bonus(1.0, 2) == 0.0

    def test_count_omitted_keeps_old_behaviour(self):
        assert tracklist_coverage_bonus(1.0) == 20.0
