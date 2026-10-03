"""get_all batches artist and track resolution into one call each."""

from kalinka_plugin_sdk.datamodel import EntityId, EntityType

import kalinka_plugin_jamendo.jamendo as jm
from kalinka_plugin_jamendo.config_model import JamendoConfig


class ArtistClient:
    """Returns an artist dict per requested id; records every request."""

    def __init__(self):
        self.calls = []

    async def request(self, path, params):
        self.calls.append((path, params))
        ids = str(params.get("id", "")).split()
        return [
            {"id": aid, "name": f"Artist {aid}", "image": f"https://img/{aid}"}
            for aid in ids
        ]


class TrackClient:
    """The /tracks index, knowing only the ids it is given; /albums/tracks
    lists every track of album 5."""

    def __init__(self, indexed):
        self.indexed = set(indexed)
        self.calls = []

    async def request(self, path, params):
        self.calls.append((path, params))
        if path == "albums/tracks":
            tracks = [{"id": "100", "name": "Listed"}, {"id": "101", "name": "Too"}]
            return [{"id": "5", "name": "Mornings", "tracks": tracks}]
        ids = str(params.get("id", "")).split()
        return [
            {"id": tid, "name": f"Track {tid}"} for tid in ids if tid in self.indexed
        ]


def _aid(local: str) -> EntityId:
    return EntityId(id=local, type=EntityType.ARTIST, source="jamendo")


def _tid(local: str) -> EntityId:
    return EntityId(id=local, type=EntityType.TRACK, source="jamendo")


async def test_tracks_resolve_in_one_request_in_the_order_asked():
    client = TrackClient(indexed={"1", "2", "3"})
    module = jm.JamendoInputModule(JamendoConfig(client_id="x"), client)

    items = await module.get_all([_tid("3"), _tid("1"), _tid("2")])

    assert [item.track.title for item in items] == ["Track 3", "Track 1", "Track 2"]
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == "tracks" and params["id"] == "3 1 2"


async def test_listed_tracks_need_no_request_and_unindexed_ones_stay():
    """The /tracks index misses many valid tracks, which still play by id."""
    client = TrackClient(indexed=set())
    module = jm.JamendoInputModule(JamendoConfig(client_id="x"), client)
    await module.browse(jm.album_id("5"), 0, 50)

    items = await module.get_all([_tid("100"), _tid("101")])
    assert [item.track.title for item in items] == ["Listed", "Too"]
    assert [path for path, _ in client.calls] == ["albums/tracks"]

    [unlisted] = await module.get_all([_tid("999")])
    assert unlisted.id == _tid("999")
    assert unlisted.track.title == ""


def _make():
    client = ArtistClient()
    return jm.JamendoInputModule(JamendoConfig(client_id="x"), client), client


async def test_artists_resolve_in_one_request():
    module, client = _make()

    items = await module.get_all([_aid("1"), _aid("2"), _aid("3")])

    assert [it.name for it in items] == ["Artist 1", "Artist 2", "Artist 3"]
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == "artists" and params["id"] == "1 2 3"


async def test_cached_artists_skip_the_request():
    module, client = _make()
    await module.get_all([_aid("1"), _aid("2")])

    # Fully cached -> no request at all; order follows the ask.
    items = await module.get_all([_aid("2"), _aid("1")])
    assert [it.name for it in items] == ["Artist 2", "Artist 1"]
    assert len(client.calls) == 1

    # Partially cached -> only the missing id is requested.
    await module.get_all([_aid("1"), _aid("9")])
    assert len(client.calls) == 2
    assert client.calls[1][1]["id"] == "9"


async def test_batch_failure_returns_what_cache_has():
    module, client = _make()
    await module.get_all([_aid("1")])

    async def boom(path, params):
        raise RuntimeError("api down")

    client.request = boom
    items = await module.get_all([_aid("1"), _aid("2")])
    # The cached artist still resolves; the unresolvable one is omitted.
    assert [it.name for it in items] == ["Artist 1"]
