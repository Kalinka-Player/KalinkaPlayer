"""The favourites endpoints: only a source that keeps favourites is asked, a
read is whole or fails by name, and a change answers what went wrong."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EntityId,
    EntityType,
    FavoriteIds,
)
from kalinka_plugin_sdk.filters import UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    ModuleCapability,
    SearchType,
    SourceUnavailableError,
)

from kalinka_server.favorite_route import register_favorite_routes


def _track(source, number):
    return EntityId(id=str(number), type=EntityType.TRACK, source=source)


class _Source(InputModule):
    """A source with one favourite track per entry of ``marked_at``, each
    marked at that time."""

    def __init__(self, name, marked_at=(), *, keeps=True, failure=None):
        self._name = name
        self._keeps = keeps
        self.marked_at = list(marked_at)
        self.failure = failure
        self.asked = []

    def module_name(self):
        return self._name

    def capabilities(self):
        return frozenset({ModuleCapability.FAVORITES} if self._keeps else ())

    def _answer(self, call):
        self.asked.append(call)
        if self.failure is not None:
            raise self.failure

    async def list_favorite(self, type, filter, offset=0, limit=50):
        self._answer(("list", type, filter, offset, limit))
        rows = [
            BrowseItem(id=_track(self._name, at), name=f"{self._name} {at}", timestamp=at)
            for at in self.marked_at
        ]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(rows),
            items=rows[offset : offset + limit],
        )

    async def get_favorite_ids(self):
        self._answer(("ids",))
        return FavoriteIds(tracks=[_track(self._name, at) for at in self.marked_at])

    async def add_to_favorite(self, id):
        self._answer(("add", id))

    async def remove_from_favorite(self, id):
        self._answer(("remove", id))


def _serving(*sources):
    """A server whose input modules are ``sources``; what it sends to the
    queue bus collects in the client's ``published``."""
    by_name = {source.module_name(): source for source in sources}

    def resolve_modules(named):
        names = list(by_name) if named is None else named.split(",")
        if any(name not in by_name for name in names):
            raise HTTPException(status_code=404, detail="No matching input modules found")
        return [by_name[name] for name in names]

    def resolve_module(entity_id):
        if entity_id.source not in by_name:
            raise HTTPException(status_code=404, detail="Input module not found")
        return by_name[entity_id.source]

    def parse_entity_id(id):
        try:
            return EntityId.from_string(id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid entity ID format: {e}")

    app = FastAPI()
    client = TestClient(app)
    client.published = []
    register_favorite_routes(
        app, resolve_modules, resolve_module, parse_entity_id, client.published.append
    )
    return client


def _ids(response):
    assert response.status_code == 200
    return response.json()["tracks"]


def _names(response):
    assert response.status_code == 200
    return [item["name"] for item in response.json()["items"]]


def test_only_a_source_that_keeps_favourites_is_asked_for_them():
    tidal, library = _Source("tidal", [5]), _Source("localfiles", [9], keeps=False)
    client = _serving(tidal, library)
    assert _ids(client.get("/favorite/ids")) == ["kalinka:tidal:track:5"]
    assert _names(client.get("/favorite/list/track")) == ["tidal 5"]
    assert library.asked == []


def test_naming_a_source_that_keeps_none_is_an_empty_answer():
    library = _Source("localfiles", [9], keeps=False)
    client = _serving(library)
    answer = client.get("/favorite/ids", params={"sources": "localfiles"}).json()
    assert answer == {"albums": [], "artists": [], "tracks": [], "playlists": []}
    page = client.get(
        "/favorite/list/track", params={"sources": "localfiles", "offset": 4, "limit": 2}
    ).json()
    assert (page["items"], page["total"], page["offset"], page["limit"]) == ([], 0, 4, 2)
    assert library.asked == []


def test_a_source_nobody_knows_is_not_an_empty_answer():
    client = _serving(_Source("tidal"))
    assert client.get("/favorite/ids", params={"sources": "nope"}).status_code == 404


def test_the_ids_of_several_sources_come_as_one_answer():
    client = _serving(_Source("tidal", [5]), _Source("qobuz", [7, 8]))
    assert _ids(client.get("/favorite/ids", params={"sources": "tidal,qobuz"})) == [
        "kalinka:tidal:track:5",
        "kalinka:qobuz:track:7",
        "kalinka:qobuz:track:8",
    ]


def test_the_earlier_name_of_the_ids_parameter_still_names_a_source():
    client = _serving(_Source("tidal", [5]), _Source("qobuz", [7]))
    assert _ids(client.get("/favorite/ids", params={"source": "qobuz"})) == [
        "kalinka:qobuz:track:7"
    ]
    both = client.get("/favorite/ids", params={"source": "qobuz", "sources": "tidal"})
    assert both.status_code == 422


def test_one_source_is_asked_for_exactly_the_page_wanted():
    tidal = _Source("tidal", range(200, 0, -1))
    client = _serving(tidal, _Source("qobuz", [7]))
    page = client.get(
        "/favorite/list/album",
        params={"sources": "tidal", "offset": 120, "limit": 30},
    ).json()
    assert tidal.asked == [("list", SearchType.album, "", 120, 30)]
    assert (len(page["items"]), page["total"]) == (30, 200)
    assert page["items"][0]["name"] == "tidal 80"


def test_several_sources_list_as_one_most_recently_marked_first():
    client = _serving(_Source("tidal", [9, 4]), _Source("qobuz", [7, 1]))
    assert _names(client.get("/favorite/list/track")) == [
        "tidal 9", "qobuz 7", "tidal 4", "qobuz 1",
    ]


@pytest.mark.parametrize("path", ["/favorite/ids", "/favorite/list/track"])
@pytest.mark.parametrize("failure,status,reason", [
    (SourceUnavailableError("Sign in to TIDAL."), 503, "Sign in to TIDAL."),
    (RuntimeError("private upstream detail"), 503, "Source unavailable"),
    (TimeoutError(), 504, "Did not answer in time"),
])
def test_a_source_that_cannot_answer_fails_the_read_by_name(
    path, failure, status, reason
):
    client = _serving(_Source("tidal", [5], failure=failure))
    response = client.get(path, params={"sources": "tidal"})
    assert response.status_code == status
    assert response.json()["detail"] == {"source": "tidal", "reason": reason}


@pytest.mark.parametrize("path", ["/favorite/ids", "/favorite/list/track"])
def test_one_failing_source_is_never_left_out_of_a_shared_answer(path):
    client = _serving(_Source("qobuz", [7]), _Source("tidal", failure=RuntimeError()))
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()["detail"]["source"] == "tidal"


def test_a_filter_a_source_cannot_apply_is_refused_not_ignored():
    refusal = UnsupportedFilter("filter", "TIDAL cannot search favourites")
    for sources in ("tidal", "tidal,qobuz"):
        client = _serving(_Source("tidal", [5], failure=refusal), _Source("qobuz", [7]))
        response = client.get(
            "/favorite/list/track", params={"sources": sources, "filter": "blue"}
        )
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "field": "filter",
            "reason": "TIDAL cannot search favourites",
        }


@pytest.mark.parametrize("call,asked", [
    (lambda client, id: client.put(f"/favorite/add/{id}"), "add"),
    (lambda client, id: client.delete(f"/favorite/remove/{id}"), "remove"),
])
@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
def test_a_change_goes_to_the_source_the_item_belongs_to(call, asked, kind):
    tidal, qobuz = _Source("tidal"), _Source("qobuz")
    id = f"kalinka:qobuz:{kind}:42"
    response = call(_serving(tidal, qobuz), id)
    assert (response.status_code, response.json()) == (200, {"message": "Ok"})
    assert (tidal.asked, qobuz.asked) == ([], [(asked, id)])


def test_a_change_that_was_made_is_announced_with_what_the_item_now_is():
    client = _serving(_Source("tidal"))
    client.put("/favorite/add/kalinka:tidal:album:10")
    client.delete("/favorite/remove/kalinka:tidal:album:10")
    assert [
        (event.id.to_string, event.favorite) for event in client.published
    ] == [("kalinka:tidal:album:10", True), ("kalinka:tidal:album:10", False)]


CHANGES = [
    lambda client, id: client.put(f"/favorite/add/{id}"),
    lambda client, id: client.delete(f"/favorite/remove/{id}"),
]


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("id,status", [
    ("not-an-id", 400),
    ("kalinka:nope:track:1", 404),
    ("kalinka:tidal:catalog:home", 422),
    ("kalinka:tidal:genre:1", 422),
    ("kalinka:localfiles:track:1", 409),
])
def test_a_change_that_cannot_be_made_never_reaches_a_source(change, id, status):
    tidal, library = _Source("tidal"), _Source("localfiles", keeps=False)
    client = _serving(tidal, library)
    assert change(client, id).status_code == status
    assert (tidal.asked, library.asked, client.published) == ([], [], [])


def test_a_source_that_keeps_none_is_named_in_the_refusal():
    client = _serving(_Source("localfiles", keeps=False))
    response = client.put("/favorite/add/kalinka:localfiles:track:1")
    assert response.json()["detail"] == {
        "source": "localfiles",
        "reason": "This source keeps no favourites",
    }


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("failure,status,detail", [
    (
        ValueError("Your own playlists cannot be favourites."),
        422,
        {"field": "id", "reason": "Your own playlists cannot be favourites."},
    ),
    (LookupError("gone"), 404, "No such item"),
    (
        SourceUnavailableError("TIDAL sign-in expired."),
        503,
        {"source": "tidal", "reason": "TIDAL sign-in expired."},
    ),
    (
        RuntimeError("private upstream detail"),
        503,
        {"source": "tidal", "reason": "Source unavailable"},
    ),
    (TimeoutError(), 504, {"source": "tidal", "reason": "Did not answer in time"}),
])
def test_a_change_the_source_refuses_answers_why(change, failure, status, detail):
    client = _serving(_Source("tidal", failure=failure))
    response = change(client, "kalinka:tidal:playlist:42")
    assert response.status_code == status
    assert response.json()["detail"] == detail
    assert client.published == []
