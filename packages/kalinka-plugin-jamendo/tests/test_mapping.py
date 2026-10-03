"""Offline unit tests for the Jamendo mapping/browse logic.

These stub JamendoClient.request so no network or client_id is needed.
"""

import pytest

from kalinka_plugin_sdk.datamodel import EntityType
from kalinka_plugin_sdk.inputmodule import DirectUrl, SearchType, TrackSource

from kalinka_plugin_jamendo.config_model import JamendoConfig, JamendoAudioFormat
from kalinka_plugin_jamendo import jamendo as jm


class FakeClient:
    """Records the last (path, params) and returns canned results."""

    def __init__(self, results):
        self._results = results
        self.calls = []

    async def request(self, path, params):
        self.calls.append((path, params))
        return self._results

    async def resolve_audio_url(self, track_id, audioformat):
        return f"https://files.test/{track_id}.{audioformat}"


class PathClient:
    """Returns canned results per endpoint path (first path segment)."""

    def __init__(self, by_path):
        self._by_path = by_path
        self.calls = []

    async def request(self, path, params):
        self.calls.append((path, params))
        return self._by_path.get(path, [])

    async def resolve_audio_url(self, track_id, audioformat):
        return f"https://files.test/{track_id}.{audioformat}"


def make_module(results, fmt=JamendoAudioFormat.FLAC):
    config = JamendoConfig(client_id="x", audio_format=fmt)
    module = jm.JamendoInputModule(config, FakeClient(results))
    return module


TRACK = {
    "id": "100",
    "name": "Sunrise",
    "duration": "212",
    "artist_id": "10",
    "artist_name": "The Band",
    "album_id": "5",
    "album_name": "Mornings",
    "album_image": "https://usercontent.jamendo.com?type=album&id=5&width=300",
    "audio": "https://prod.jamendo.com/?trackid=100&format=flac",
}

ALBUM = {
    "id": "5",
    "name": "Mornings",
    "artist_id": "10",
    "artist_name": "The Band",
    "image": "https://usercontent.jamendo.com?type=album&id=5&width=300",
}

ARTIST = {
    "id": "10",
    "name": "The Band",
    "image": "https://usercontent.jamendo.com?type=artist&id=10&width=300",
}

PLAYLIST = {
    "id": "7",
    "name": "Chill",
    "user_id": "99",
    "user_name": "dj",
    "tracks": [TRACK, TRACK],
}


def test_format_code_mapping():
    m = make_module([], fmt=JamendoAudioFormat.MP3_96)
    assert m.audio_format == "mp31"
    assert m.audio_mime == "audio/mpeg"


@pytest.mark.asyncio
async def test_search_tracks():
    m = make_module([TRACK])
    res = await m.search(SearchType.track, "sunrise", offset=0, limit=50)
    assert res.items[0].track.title == "Sunrise"
    assert res.items[0].track.id.type == EntityType.TRACK
    assert res.items[0].track.id.source == "jamendo"
    assert res.items[0].subname == "The Band"
    # audioformat must be forwarded for track search
    assert m.client.calls[0][1]["audioformat"] == "flac"


@pytest.mark.asyncio
async def test_search_tracks_ask_for_and_carry_their_genres():
    tagged = {**TRACK, "musicinfo": {"tags": {"genres": ["rock", "indie", "triphop"]}}}
    m = make_module([tagged])
    res = await m.search(SearchType.track, "sunrise")
    assert m.client.calls[0][1]["include"] == "musicinfo"
    genres = res.items[0].track.album.genres
    assert [genre.name for genre in genres] == ["Rock", "Indie", "Triphop"]
    assert [genre.id.id for genre in genres] == ["rock", "indie", "triphop"]
    assert {genre.id.type for genre in genres} == {EntityType.GENRE}


@pytest.mark.asyncio
async def test_a_track_without_tags_has_no_genre():
    m = make_module([TRACK])
    res = await m.search(SearchType.track, "sunrise")
    assert res.items[0].track.album.genres == []


@pytest.mark.asyncio
async def test_search_albums_have_sections():
    m = make_module([ALBUM])
    res = await m.search(SearchType.album, "mornings")
    item = res.items[0]
    assert item.can_browse is True
    assert item.album.title == "Mornings"
    assert item.sections and item.sections[0].name == "Tracks"


@pytest.mark.asyncio
async def test_estimated_total_full_page_bumps():
    # A full page (count == limit) should advertise more pages.
    m = make_module([TRACK] * 2)
    res = await m.search(SearchType.track, "x", offset=0, limit=2)
    assert res.total == 4  # offset(0) + count(2) + limit(2)


@pytest.mark.asyncio
async def test_root_catalog():
    m = make_module([])
    root = jm.catalog_id("root")
    res = await m.browse(root)
    slugs = [i.id.id for i in res.items]
    assert slugs == ["popular", "new-releases", "featured-playlists"]
    # Root is built locally, no API call.
    assert m.client.calls == []


@pytest.mark.asyncio
async def test_root_catalog_shelves_have_subname():
    # Every shelf carries a human description on both the BrowseItem (subname)
    # and the Catalog (description); the frontend renders
    # ``catalog.description ?? item.subname``.
    m = make_module([])
    res = await m.browse(jm.catalog_id("root"))
    for item in res.items:
        assert item.subname, f"{item.id.id} has no subname"
        assert item.catalog.description == item.subname


@pytest.mark.asyncio
async def test_new_releases_bounds_release_date_sort():
    # New Releases must constrain the release-date sort to a recent window,
    # otherwise Jamendo's unbounded sort is ~5s and times out.
    m = make_module([ALBUM])
    res = await m.browse(jm.catalog_id("new-releases"), offset=0, limit=20)
    assert len(res.items) == 1
    path, params = m.client.calls[-1]
    assert path == "albums"
    assert params["order"] == "releasedate_desc"
    # datebetween is "<start>_<end>"; the window matches the configured span.
    start, end = params["datebetween"].split("_")
    span = jm.date.fromisoformat(end) - jm.date.fromisoformat(start)
    assert span.days == jm.NEW_RELEASES_WINDOW_DAYS


@pytest.mark.asyncio
async def test_browse_album_paginates_tracks():
    album_with_tracks = {**ALBUM, "tracks": [TRACK, TRACK, TRACK]}
    m = make_module([album_with_tracks])
    res = await m.browse(jm.album_id("5"), offset=1, limit=1)
    assert res.total == 3
    assert len(res.items) == 1


@pytest.mark.asyncio
async def test_album_track_inherits_artist_from_album():
    # Real Jamendo quirk: /albums/tracks nests tracks under the album and the
    # nested track objects carry NO artist_id/artist_name (the artist lives on
    # the album). Without the album_meta fallback these tracks reach the
    # playqueue with an empty artist even though browse shows the album artist.
    bare_track = {
        "id": "100",
        "name": "Sunrise",
        "duration": "212",
        "position": "1",
    }
    album_with_tracks = {**ALBUM, "tracks": [bare_track]}
    m = make_module([album_with_tracks])
    res = await m.browse(jm.album_id("5"), 0, 50)
    perf = res.items[0].track.performer
    assert perf.id.id == "10"
    assert perf.name == "The Band"
    # And it is cached so a queue-add via get_track_info keeps the artist.
    infos = await m.get_track_info(["100"])
    assert infos[0].metadata.performer.id.id == "10"
    assert infos[0].metadata.performer.name == "The Band"


@pytest.mark.asyncio
async def test_browse_artist_uses_albums_endpoint():
    # Artist browse goes through /albums/?artist_id= so cards get the real
    # trackid-bearing cover; offset/limit are forwarded for native pagination.
    m = make_module([ALBUM, ALBUM])
    res = await m.browse(jm.artist_id("10"), offset=5, limit=2)
    path, params = m.client.calls[0]
    assert path == "albums"
    assert params["artist_id"] == "10"
    assert params["offset"] == 5 and params["limit"] == 2
    assert res.items[0].album.title == "Mornings"


@pytest.mark.asyncio
async def test_get_track_info_preserves_order_and_url():
    t2 = {**TRACK, "id": "200", "name": "Dusk"}
    m = make_module([TRACK, t2])
    infos = await m.get_track_info(["200", "100"])
    assert [i.id.id for i in infos] == ["200", "100"]
    source = await infos[0].source_retriever()
    assert isinstance(source, TrackSource)
    assert source.format == "audio/flac"
    # Jamendo serves its own files, so the renderer fetches them directly.
    assert source.source == DirectUrl(url="https://files.test/200.flac")


@pytest.mark.asyncio
async def test_get_track_info_metadata_from_cache_then_index():
    # Metadata for an id absent from the /tracks/ index still comes through,
    # because the browse cache supplies it. Playback URLs always resolve via
    # the file endpoint regardless.
    album_with_tracks = {**ALBUM, "tracks": [TRACK]}
    config = JamendoConfig(client_id="x", audio_format=JamendoAudioFormat.MP3_VBR)
    client = PathClient({"albums/tracks": [album_with_tracks], "tracks": []})
    m = jm.JamendoInputModule(config, client)

    # Cold: id not in cache and /tracks/ returns nothing -> placeholder metadata,
    # but still playable via the file endpoint.
    cold = await m.get_track_info(["100"])
    assert cold[0].metadata.title == ""
    cold_source = await cold[0].source_retriever()
    assert cold_source.source.url == "https://files.test/100.mp32"

    # After browsing the album, metadata comes from the cache.
    await m.browse(jm.album_id("5"), 0, 50)
    infos = await m.get_track_info(["100"])
    assert infos[0].metadata.title == "Sunrise"
    # The /tracks/ index is not even queried for a cached id.
    assert not any(
        path == "tracks" for path, _ in client.calls[1:]
    )


@pytest.mark.asyncio
async def test_link_resolves_via_file_endpoint_honoring_format():
    # The playback URL always comes from /tracks/file/ (here stubbed), honouring
    # the configured format and its mime (which selects the decoder).
    config = JamendoConfig(client_id="x", audio_format=JamendoAudioFormat.FLAC)
    client = PathClient({"tracks": []})
    m = jm.JamendoInputModule(config, client)
    infos = await m.get_track_info(["999"])
    source = await infos[0].source_retriever()
    assert source.source.url == "https://files.test/999.flac"
    assert source.format == "audio/flac"


@pytest.mark.asyncio
async def test_link_raises_when_url_unresolved():
    # If the file endpoint can't resolve a URL, source_retriever must raise so the
    # server marks the track unavailable instead of trying to play "".
    class NoUrlClient(PathClient):
        async def resolve_audio_url(self, track_id, audioformat):
            return ""

    config = JamendoConfig(client_id="x", audio_format=JamendoAudioFormat.MP3_VBR)
    m = jm.JamendoInputModule(config, NoUrlClient({"tracks": []}))
    infos = await m.get_track_info(["999"])
    with pytest.raises(RuntimeError):
        await infos[0].source_retriever()
    with pytest.raises(RuntimeError):
        await m.get_track_source("999")


@pytest.mark.asyncio
async def test_track_source_comes_from_the_id_alone():
    # Played from a queue restored before anything was listed: no metadata
    # lookup, just the file endpoint in the configured format.
    config = JamendoConfig(client_id="x", audio_format=JamendoAudioFormat.FLAC)
    client = PathClient({})
    m = jm.JamendoInputModule(config, client)
    source = await m.get_track_source("999")
    assert source == TrackSource(
        source=DirectUrl(url="https://files.test/999.flac"), format="audio/flac"
    )
    assert client.calls == []


@pytest.mark.asyncio
async def test_request_degrades_on_non_json(monkeypatch):
    # A non-JSON body (HTML error page, truncated response) degrades to an empty
    # result set rather than raising.
    from kalinka_plugin_jamendo.jamendo import JamendoClient

    client = JamendoClient("x")

    class FakeResp:
        is_success = True

        def json(self):
            raise ValueError("not json")

    async def fake_get(url, params=None):
        return FakeResp()

    monkeypatch.setattr(client.session, "get", fake_get)
    assert await client.request("tracks", {"id": "1"}) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_get_playlist():
    m = make_module([PLAYLIST])
    item = await m.get(jm.playlist_id("7"))
    assert item.playlist.track_count == 2
    assert item.playlist.owner.name == "dj"


@pytest.mark.asyncio
async def test_unsupported_get_type_raises():
    m = make_module([])
    with pytest.raises(ValueError):
        await m.get(jm.user_id("1"))


@pytest.mark.asyncio
async def test_get_state_reports_missing_client_id():
    from types import SimpleNamespace

    from kalinka_plugin_sdk import ModuleHealthState
    from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo

    plugin = KalinkaPluginJamendo()

    # No context yet (not set up) -> treated as missing key -> ERROR.
    state = await plugin.get_state()
    assert state.state == ModuleHealthState.ERROR
    assert "client_id" in state.message

    # Empty client_id -> ERROR with the registration URL.
    plugin._context = SimpleNamespace(config=SimpleNamespace(client_id=""))
    state = await plugin.get_state()
    assert state.state == ModuleHealthState.ERROR
    assert "devportal.jamendo.com" in state.message

    # Configured client_id -> READY.
    plugin._context = SimpleNamespace(config=SimpleNamespace(client_id="abc"))
    state = await plugin.get_state()
    assert state.state == ModuleHealthState.READY


@pytest.mark.asyncio
async def test_stubs_are_graceful():
    m = make_module([])
    assert (await m.get_favorite_ids()).tracks == []
    assert (await m.list_favorite(SearchType.track, "")).total == 0
    assert (await m.playlist_user_list()).total == 0
    assert await m.get_resource_path("x") is None
