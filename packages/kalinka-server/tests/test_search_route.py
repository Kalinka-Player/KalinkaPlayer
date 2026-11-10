"""The search endpoints: one source at a time, hits arriving annotated, and a
source that fails answering 503 by name."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kalinka_plugin_sdk.datamodel import (
    Artist,
    BrowseItem,
    BrowseItemList,
    EntityId,
    EntityType,
)
from kalinka_plugin_sdk.inputmodule import InputModule, SearchType

from kalinka_server.config_model import SearchConfig
from kalinka_server.search_route import register_search_routes


class _Module(InputModule):
    def __init__(self, name, failing=False):
        self._name = name
        self._failing = failing
        self.suggested = []

    def module_name(self):
        return self._name

    async def search(self, type, query, offset=0, limit=50):
        if self._failing:
            raise RuntimeError("upstream down")
        if type is not SearchType.artist:
            return BrowseItemList(offset=offset, limit=limit, total=0, items=[])
        aid = EntityId(id="1", type=EntityType.ARTIST, source=self._name)
        item = BrowseItem(
            id=aid,
            name="The Beatles",
            can_browse=True,
            artist=Artist(id=aid, name="The Beatles"),
        )
        return BrowseItemList(offset=offset, limit=limit, total=1, items=[item])

    async def ai_search(self, query, offset=0, limit=50):
        self.suggested.append(limit)
        if self._failing:
            raise RuntimeError("upstream down")
        return BrowseItemList(offset=offset, limit=limit, total=0, items=[])


MODULES = {"localfiles": _Module("localfiles"), "qobuz": _Module("qobuz", failing=True)}


def _resolve(sources):
    names = sources.split(",") if sources else list(MODULES)
    unknown = [name for name in names if name not in MODULES]
    if unknown:
        raise HTTPException(status_code=404, detail="No matching input modules found")
    return [MODULES[name] for name in names]


def _serving(module, config=SearchConfig):
    """A server whose every source is ``module``."""
    app = FastAPI()
    register_search_routes(app, lambda _: [module], lambda _: [module], config)
    return TestClient(app)


@pytest.fixture
def client():
    app = FastAPI()
    register_search_routes(app, _resolve, _resolve, SearchConfig)
    return TestClient(app)


def test_matches_arrive_annotated(client):
    response = client.get(
        "/search/matches", params={"query": "the beatles", "sources": "localfiles"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["match"] == {
        "tier": "exact",
        "score": 100.0,
        "relevance": 1.0,
    }


def test_a_failing_source_is_503_naming_it(client):
    response = client.get(
        "/search/matches", params={"query": "the beatles", "sources": "qobuz"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["source"] == "qobuz"


def test_suggestions_fail_the_same_way(client):
    ok = client.get("/ai_search", params={"query": "calm", "sources": "localfiles"})
    down = client.get("/ai_search", params={"query": "calm", "sources": "qobuz"})
    assert (ok.status_code, down.status_code) == (200, 503)


@pytest.mark.parametrize("route", ["/search/matches", "/ai_search"])
def test_a_source_must_be_named(client, route):
    """Omitting it used to mean every source at once, which is an answer no
    client can lay out a piece at a time."""
    assert client.get(route, params={"query": "calm"}).status_code == 422


def test_suggestions_are_asked_of_one_source(client):
    response = client.get(
        "/ai_search", params={"query": "calm", "sources": "localfiles,qobuz"}
    )

    assert response.status_code == 400


def test_the_configured_limit_is_what_reaches_the_source():
    """The caller does not size the answer: relevance falls away past the
    first few dozen, so there is nothing to page to."""
    module = _Module("localfiles")

    _serving(module, lambda: SearchConfig(ai_suggestions_limit=7)).get(
        "/ai_search", params={"query": "calm", "sources": "localfiles"}
    )

    assert module.suggested == [7]


def test_a_blank_query_asks_nothing():
    module = _Module("localfiles")

    response = _serving(module).get(
        "/ai_search", params={"query": "   ", "sources": "localfiles"}
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert module.suggested == []


def test_an_unknown_source_is_404(client):
    response = client.get(
        "/search/matches", params={"query": "the beatles", "sources": "nope"}
    )
    assert response.status_code == 404


def test_a_name_reaches_a_source_that_suggests_nothing():
    """Collections are searched by name but have no audio to suggest: the
    suggestion leg answers empty rather than reporting a missing source."""
    app = FastAPI()
    register_search_routes(
        app,
        lambda sources: [_Module("collections")],
        lambda sources: [],
        SearchConfig,
    )
    client = TestClient(app)

    matches = client.get(
        "/search/matches", params={"query": "the beatles", "sources": "collections"}
    )
    suggestions = client.get(
        "/ai_search", params={"query": "the beatles", "sources": "collections"}
    )

    assert matches.json()["total"] == 1
    assert suggestions.status_code == 200
    assert suggestions.json()["total"] == 0


def test_a_hit_carries_the_art_a_browse_would_have_given_it():
    """The server composes a cover for a catalog that has none, on its way
    out. It only ever ran on the browse path, so a collection found by name
    arrived bare and the client drew its own."""
    decorated = []

    def decorate(result):
        decorated.append(result)
        for item in result.items:
            item.name = f"{item.name} (decorated)"

    app = FastAPI()
    register_search_routes(
        app,
        lambda _: [_Module("collections")],
        lambda _: [],
        SearchConfig,
        decorate,
    )
    client = TestClient(app)

    body = client.get(
        "/search/matches", params={"query": "the beatles", "sources": "collections"}
    ).json()

    assert len(decorated) == 1
    assert body["items"][0]["name"] == "The Beatles (decorated)"
