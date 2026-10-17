"""Offline unit tests for JamendoInputModule.ai_search.

A fake mood index supplies ranked (track_id, distance) hits and a fake client
supplies metadata, so no model, sqlite-vec db, or network is needed.
"""

import pytest

from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo import jamendo as jm


class FakeMoodIndex:
    """Returns a fixed ranked list, truncated to the requested limit."""

    def __init__(self, ranked_ids):
        # ranked best-first; distance just increases so order is unambiguous.
        self._hits = [(int(t), 0.1 * i) for i, t in enumerate(ranked_ids)]

    async def search(self, query, limit):
        return self._hits[:limit]


class MetadataClient:
    """Returns a track dict per requested id, omitting any in `missing`."""

    def __init__(self, missing=()):
        self.missing = set(str(m) for m in missing)
        self.calls = []

    async def request(self, path, params):
        self.calls.append((path, params))
        ids = str(params.get("id", "")).split()
        return [
            {
                "id": tid,
                "name": f"Track {tid}",
                "duration": "200",
                "artist_id": "1",
                "artist_name": "Artist",
                "album_id": "9",
                "album_name": "Album",
                "album_image": "https://img/9",
            }
            for tid in ids
            if tid not in self.missing
        ]

    async def resolve_audio_url(self, track_id, audioformat):
        return f"https://files.test/{track_id}"


RANKED = ["100", "200", "300", "400", "500", "600"]


def make_module(client, ranked=RANKED):
    cfg = JamendoConfig(client_id="x")
    return jm.JamendoInputModule(cfg, client, FakeMoodIndex(ranked))


def _card_tracks(res):
    """ai_search returns a single AI-suggestions card; its tracks are in
    ``sections``."""
    assert len(res.items) == 1
    card = res.items[0]
    assert card.name == "DISCOVER ON JAMENDO"
    return card.sections


@pytest.mark.asyncio
async def test_returns_tracks_in_mood_order():
    client = MetadataClient()
    mod = make_module(client)
    res = await mod.ai_search("melancholic", offset=0, limit=3)
    tracks = _card_tracks(res)
    assert [it.id.id for it in tracks] == ["100", "200", "300"]
    # tracks only — no album/artist browse items
    for it in tracks:
        assert it.track is not None
        assert it.album is None and it.artist is None
        assert it.can_add is True
    # one batch metadata call to the tracks endpoint
    assert client.calls and client.calls[0][0] == "tracks"
    assert client.calls[0][1]["include"] == "musicinfo"


@pytest.mark.asyncio
async def test_pagination_non_overlapping():
    mod = make_module(MetadataClient())
    p1 = await mod.ai_search("q", offset=0, limit=3)
    p2 = await mod.ai_search("q", offset=3, limit=3)
    ids1 = [it.id.id for it in _card_tracks(p1)]
    ids2 = [it.id.id for it in _card_tracks(p2)]
    assert ids1 == ["100", "200", "300"]
    assert ids2 == ["400", "500", "600"]
    assert not set(ids1) & set(ids2)


@pytest.mark.asyncio
async def test_missing_metadata_dropped_order_preserved():
    # API can't resolve 200 -> it's dropped, the rest keep mood order.
    mod = make_module(MetadataClient(missing=["200"]))
    res = await mod.ai_search("q", offset=0, limit=3)
    assert [it.id.id for it in _card_tracks(res)] == ["100", "300"]


def test_available_follows_the_index():
    cfg = JamendoConfig(client_id="x")
    assert make_module(MetadataClient()).ai_search_available()
    assert not jm.JamendoInputModule(
        cfg, MetadataClient(), mood_index=None
    ).ai_search_available()


@pytest.mark.asyncio
async def test_disabled_when_no_index():
    cfg = JamendoConfig(client_id="x")
    mod = jm.JamendoInputModule(cfg, MetadataClient(), mood_index=None)
    res = await mod.ai_search("q")
    assert res.items == [] and res.total == 0


@pytest.mark.asyncio
async def test_blank_query_returns_empty():
    client = MetadataClient()
    mod = make_module(client)
    res = await mod.ai_search("   ")
    assert res.items == []
    assert client.calls == []  # no API call for a blank query
