"""The favourites endpoints as the production app mounts them, over its own
resolution of sources and ids."""

import pytest

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running

NOTHING_MARKED = {"albums": [], "artists": [], "tracks": [], "playlists": []}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    app = await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})
    async with running(app) as value:
        yield value


async def test_a_server_whose_sources_keep_none_answers_no_favourites(client):
    ids = await client.get("/favorite/ids")
    assert (ids.status_code, ids.json()) == (200, NOTHING_MARKED)
    page = await client.get("/favorite/list/track")
    assert (page.status_code, page.json()["items"]) == (200, [])


async def test_a_browse_only_source_has_no_favourites_rather_than_being_unknown(client):
    ids = await client.get("/favorite/ids", params={"sources": "collections"})
    assert (ids.status_code, ids.json()) == (200, NOTHING_MARKED)
    assert (
        await client.get("/favorite/ids", params={"sources": "nope"})
    ).status_code == 404


@pytest.mark.parametrize(
    "id,status",
    [
        ("track-1", 400),
        ("kalinka:nope:track:1", 404),
        ("kalinka:nope:catalog:root", 422),
    ],
)
async def test_a_change_is_refused_by_what_is_wrong_with_its_id(client, id, status):
    assert (await client.put(f"/favorite/add/{id}")).status_code == status
    assert (await client.delete(f"/favorite/remove/{id}")).status_code == status
