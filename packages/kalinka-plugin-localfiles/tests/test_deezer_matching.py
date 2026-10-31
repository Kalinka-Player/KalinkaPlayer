#!/usr/bin/env python3
"""How an album is found on Deezer, and how a candidate is judged.

Three shipped defects met here. The structured query sent its values
unquoted, which Deezer answers with nothing at all for any multi-word name.
The fallback query then searched on the title alone, so the artist could not
be among the results it went on to demand an artist match from. And ranking
was on the title score alone, so a covers act with an identical title
outranked the real release and was then rejected by the artist test — losing
the correct match that was sitting in the same response.
"""

import io

import pytest
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.enricher.deezer_plugin import DeezerPlugin


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b"", is_redirect=False):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        # A redirect is how the CDN says it has only a generic placeholder.
        self.is_redirect = is_redirect

    def json(self):
        return self._payload


def _jpeg_bytes():
    """A real image, because the plugin decodes what it downloads."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (90, 120, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


class RecordingClient:
    """Answers each search from a queue and remembers the query strings.

    Anything that is not a search is the cover download, which succeeds.
    """

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.queries = []
        self.fetched = []

    async def get(self, url, params=None):
        if params and "q" in params:
            self.queries.append(params["q"])
            payload = self._payloads.pop(0) if self._payloads else {"data": []}
            return FakeResponse(payload)
        self.fetched.append(url)
        return FakeResponse({}, content=_jpeg_bytes())


class _NoClaims:
    async def get_claims(self, entity_type, entity_id, field):
        return []

    async def get_artist_by_id(self, artist_id):
        return None

    async def get_album_by_id(self, album_id):
        return {"id": album_id}

    async def set_album_image(self, album_id, generated):
        pass


def _plugin(tmp_path) -> DeezerPlugin:
    config = LocalFilesConfig(
        db_path=str(tmp_path / "db.sqlite"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return DeezerPlugin(config, db_manager=_NoClaims())


def _album(title, artist, cover="https://cdn/cover.jpg"):
    entry = {"title": title, "artist": {"name": artist}}
    if cover:
        entry["cover_xl"] = cover
    return entry


ABBEY_ROAD = {"id": "album_1", "title": "Abbey Road", "artist_name": "The Beatles"}


class TestTheQueriesSent:
    @pytest.mark.asyncio
    async def test_the_structured_query_quotes_its_values(self, tmp_path):
        """Deezer returns nothing at all for an unquoted multi-word value, so
        this shape has never matched a real artist."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient({"data": []}, {"data": []})

        await p.enrich_album(dict(ABBEY_ROAD))

        assert p.async_client.queries[0] == 'artist:"The Beatles" album:"Abbey Road"'

    @pytest.mark.asyncio
    async def test_the_fallback_query_names_the_artist(self, tmp_path):
        """Searching the title alone returns other people's records, and the
        artist test then rejects every one of them."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient({"data": []}, {"data": []})

        await p.enrich_album(dict(ABBEY_ROAD))

        assert p.async_client.queries[1] == "The Beatles Abbey Road"

    @pytest.mark.asyncio
    async def test_a_match_on_the_first_query_stops_there(self, tmp_path):
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            {"data": [_album("Abbey Road", "The Beatles")]}
        )
        p._save_images = lambda *a: True

        assert await p.enrich_album(dict(ABBEY_ROAD)) is not None
        assert len(p.async_client.queries) == 1


    @pytest.mark.asyncio
    async def test_a_cover_it_found_is_not_left_flagged_as_generated(
        self, tmp_path
    ):
        """It runs on an album carrying generated art, so a row it fills that
        still says generated has its real cover wiped by the art sweep."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            {"data": [_album("Abbey Road", "The Beatles")]}
        )
        p._save_images = lambda *a: True

        result = await p.enrich_album(
            dict(ABBEY_ROAD, image_url="al1", image_generated=1)
        )

        assert result["updates"]["image_url"]
        assert result["updates"]["image_generated"] == 0


class TestJudgingCandidates:
    def test_the_real_artist_beats_a_higher_scoring_cover_act(self, tmp_path):
        """The Abbey Road case: the tribute band's title is a perfect match
        and the Beatles' is not, because theirs says "(Remastered)"."""
        p = _plugin(tmp_path)
        candidates = [
            _album("Abbey Road (Remastered)", "The Beatles"),
            _album("Abbey Road", "The Beatles Complete On Ukulele"),
            _album("Abbey Road", "Rock4"),
        ]

        match = p._find_best_album_match(candidates, "Abbey Road", "The Beatles")

        assert match is not None
        assert match[0]["artist"]["name"] == "The Beatles"

    def test_a_sequel_in_roman_numerals_is_the_same_record(self, tmp_path):
        p = _plugin(tmp_path)
        candidates = [_album("Fairytale II", "Zero-Project")]

        match = p._find_best_album_match(candidates, "Fairytale 2", "zero-project")

        assert match is not None and match[0]["title"] == "Fairytale II"

    def test_a_truncated_artist_tag_still_matches(self, tmp_path):
        """Legacy fixed-width tag fields cut names off mid-word."""
        p = _plugin(tmp_path)
        candidates = [_album("Твои письма", "Иванушки International")]

        match = p._find_best_album_match(
            candidates, "Твои письма", "Иванушки Int"
        )

        assert match is not None

    def test_the_right_title_by_the_wrong_artist_is_refused(self, tmp_path):
        p = _plugin(tmp_path)
        candidates = [_album("Abbey Road", "Booker T. & The MG's")]

        assert p._find_best_album_match(candidates, "Abbey Road", "The Beatles") is None

    def test_the_right_artist_with_an_unrelated_title_is_refused(self, tmp_path):
        p = _plugin(tmp_path)
        candidates = [_album("Let It Be", "The Beatles")]

        assert p._find_best_album_match(candidates, "Abbey Road", "The Beatles") is None

    def test_a_candidate_without_a_title_is_ignored(self, tmp_path):
        p = _plugin(tmp_path)
        candidates = [
            {"artist": {"name": "The Beatles"}},
            _album("Abbey Road", "The Beatles"),
        ]

        match = p._find_best_album_match(candidates, "Abbey Road", "The Beatles")

        assert match is not None and match[0]["title"] == "Abbey Road"

    def test_nothing_at_all_is_no_match(self, tmp_path):
        p = _plugin(tmp_path)
        assert p._find_best_album_match([], "Abbey Road", "The Beatles") is None


def _deezer_artist(name, fans, picture_id):
    return {
        "id": picture_id,
        "name": name,
        "nb_fan": fans,
        "picture_xl": f"https://cdn.example/{picture_id}/1000x1000.jpg",
    }


class TestTwoArtistsOfOneName:
    """Deezer lists both the composer Vangelis and a Greek singer of the same
    name. Every name score is 1.00, so the name cannot choose and the answer
    order was deciding — which put a Christmas single's sleeve on a library's
    Vangelis. Audience size cannot choose either: a library may genuinely hold
    the obscure one. So nothing here chooses, and the artist is left to a
    source that works from its identity.
    """

    def _search(self, *artists):
        return {"data": list(artists)}

    @pytest.mark.asyncio
    async def test_an_exact_tie_is_declined_rather_than_guessed(self, tmp_path):
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._search(
                _deezer_artist("Vangelis", 26, "impostor"),
                _deezer_artist("Vangelis", 210183, "composer"),
            )
        )

        assert await p.enrich_artist({"id": "artist_1", "name": "Vangelis"}) is None
        assert p.async_client.fetched == []

    @pytest.mark.asyncio
    async def test_popularity_does_not_break_the_tie(self, tmp_path):
        """The obscure act is somebody's favourite; fan counts say who is
        famous, never who this library holds."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._search(
                _deezer_artist("Vangelis", 210183, "composer"),
                _deezer_artist("Vangelis", 26, "impostor"),
            )
        )

        assert await p.enrich_artist({"id": "artist_1", "name": "Vangelis"}) is None

    @pytest.mark.asyncio
    async def test_one_clear_winner_is_still_taken(self, tmp_path):
        """Declining a tie must not make the plugin useless where the name
        does in fact single one act out."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._search(
                _deezer_artist("Vangelis Papathanassiou", 244, "near"),
                _deezer_artist("Jon & Vangelis", 8803, "duo"),
            )
        )

        result = await p.enrich_artist(
            {"id": "artist_1", "name": "Vangelis Papathanassiou"}
        )

        assert result == {"updates": {"image_url": "artist_1"}}
        assert p.async_client.fetched == ["https://cdn.example/near/1000x1000.jpg"]

    @pytest.mark.asyncio
    async def test_a_single_result_is_not_a_tie(self, tmp_path):
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._search(_deezer_artist("Vangelis", 26, "only"))
        )

        assert await p.enrich_artist(
            {"id": "artist_1", "name": "Vangelis"}
        ) == {"updates": {"image_url": "artist_1"}}


class TestTheArtistWithNoPhotograph:
    """Deezer answers "no photograph" with the MD5 of the empty string where
    the image hash belongs. It serves that two ways — already spelled out, or
    as an empty path segment redirecting to it — and only the second is a
    redirect. The first is a plain 200 carrying a grey silhouette, which is
    how three artists in a real library came to share one picture.
    """

    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"

    def _artist(self, picture):
        return {"data": [{"id": "x", "name": "Emcee Lynx", "picture_xl": picture}]}

    @pytest.mark.asyncio
    async def test_the_spelled_out_placeholder_is_refused(self, tmp_path):
        """The form a redirect check cannot see."""
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._artist(
                f"https://cdn-images.dzcdn.net/images/artist/{self._EMPTY_MD5}"
                "/1000x1000-000000-80-0-0.jpg"
            )
        )

        assert await p.enrich_artist({"id": "artist_1", "name": "Emcee Lynx"}) is None
        assert p.async_client.fetched == []  # not even downloaded

    @pytest.mark.asyncio
    async def test_the_empty_segment_placeholder_is_refused(self, tmp_path):
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._artist(
                "https://cdn-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"
            )
        )

        assert await p.enrich_artist({"id": "artist_1", "name": "Emcee Lynx"}) is None
        assert p.async_client.fetched == []

    @pytest.mark.asyncio
    async def test_a_real_photograph_is_still_taken(self, tmp_path):
        p = _plugin(tmp_path)
        p.async_client = RecordingClient(
            self._artist(
                "https://cdn-images.dzcdn.net/images/artist/"
                "2673c450edb7948b32c537dd8a9fec2e/1000x1000-000000-80-0-0.jpg"
            )
        )

        result = await p.enrich_artist({"id": "artist_1", "name": "Emcee Lynx"})

        assert result == {"updates": {"image_url": "artist_1"}}
        assert len(p.async_client.fetched) == 1

    @pytest.mark.asyncio
    async def test_a_redirect_is_still_refused_as_a_backstop(self, tmp_path):
        """Whatever else the CDN redirects to, it is not this artist."""
        p = _plugin(tmp_path)
        client = RecordingClient(
            self._artist("https://cdn-images.dzcdn.net/images/artist/abc/1000.jpg")
        )
        original = client.get

        async def redirecting(url, params=None):
            response = await original(url, params=params)
            if not (params and "q" in params):
                response.is_redirect = True
            return response

        client.get = redirecting
        p.async_client = client

        assert await p.enrich_artist({"id": "artist_1", "name": "Emcee Lynx"}) is None
