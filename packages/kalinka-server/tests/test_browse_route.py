"""The browse endpoints: a filter document reaches the module intact, and a
refusal reaches the caller instead of an unfiltered listing."""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EntityId,
    EntityType,
)
from kalinka_plugin_sdk.filters import (
    FilterQuery,
    FilterValue,
    FilterValueList,
    UnsupportedFilter,
)

from kalinka_server.browse_route import register_browse_routes

CATALOG = "kalinka:testsource:catalog:albums"


class _FakeModule:
    """Records what the route handed it; refuses the fields it never offered."""

    declared = {"q", "genre"}

    def __init__(self):
        self.browsed = []
        self.asked = []

    async def browse(self, entity_id, offset=0, limit=50, filter=FilterQuery({})):
        filter.reject_undeclared(self.declared)
        self.browsed.append((entity_id, offset, limit, filter))
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=1,
            items=[
                BrowseItem(
                    id=EntityId(id="a1", type=EntityType.ALBUM, source="testsource"),
                    name="An Album",
                    can_browse=True,
                    can_add=True,
                )
            ],
        )

    async def list_filter_values(
        self, catalog_id, field, offset=0, limit=50, q=""
    ) -> FilterValueList:
        if field not in self.declared:
            raise UnsupportedFilter(field, "no such vocabulary here")
        self.asked.append((catalog_id, field, offset, limit, q))
        return FilterValueList(
            offset=offset,
            limit=limit,
            total=1,
            items=[FilterValue(id="jazz", name="Jazz", count=3)],
        )


class _BrokenModule(_FakeModule):
    async def browse(self, *args, **kwargs):
        raise RuntimeError("backend on fire")


@pytest.fixture
def module():
    return _FakeModule()


@pytest.fixture
def client(module):
    decorated = []

    def resolve(entity_id: EntityId):
        if entity_id.source != "testsource":
            raise HTTPException(status_code=404, detail="Input module not found")
        return module

    def list_roots(offset, limit):
        listed_roots.append((offset, limit))
        return BrowseItemList(offset=offset, limit=limit, total=0, items=[])

    listed_roots = []
    app = FastAPI()
    register_browse_routes(
        app,
        resolve,
        EntityId.from_string,
        list_roots,
        lambda result: decorated.append(result),
    )
    client = TestClient(app)
    client.decorated = decorated
    client.listed_roots = listed_roots
    return client


def _filter(document) -> dict:
    return {"filter": json.dumps(document)}


def test_browse_without_a_filter_is_the_unfiltered_listing(client, module):
    r = client.get(f"/browse/{CATALOG}")

    assert r.status_code == 200
    assert r.json()["items"][0]["name"] == "An Album"
    _, _, _, filter = module.browsed[0]
    assert filter.fields() == set()


def test_the_document_reaches_the_module_intact(client, module):
    r = client.get(
        f"/browse/{CATALOG}",
        params=_filter({"q": {"contains": "miles"}, "genre": {"any": ["jazz"]}}),
    )

    assert r.status_code == 200
    _, _, _, filter = module.browsed[0]
    assert filter.text() == "miles"
    assert filter.values("genre").any == ["jazz"]


def test_pagination_travels_alongside_the_filter(client, module):
    client.get(
        f"/browse/{CATALOG}",
        params={"offset": 20, "limit": 5, **_filter({"q": {"contains": "x"}})},
    )

    _, offset, limit, _ = module.browsed[0]
    assert (offset, limit) == (20, 5)


def test_a_result_still_goes_through_the_servers_own_pass(client):
    client.get(f"/browse/{CATALOG}")
    assert len(client.decorated) == 1


@pytest.mark.parametrize(
    "document",
    [{"genre": {}}, {"genre": {"anny": ["jazz"]}}, {"year": {"gte": 2000, "lte": 1990}}],
)
def test_a_malformed_document_is_refused_before_the_module(client, module, document):
    r = client.get(f"/browse/{CATALOG}", params=_filter(document))

    assert r.status_code == 422
    assert module.browsed == []


def test_filter_that_is_not_json_at_all_is_refused(client, module):
    r = client.get(f"/browse/{CATALOG}", params={"filter": "genre=jazz"})

    assert r.status_code == 422
    assert module.browsed == []


def test_a_shape_error_names_its_field_like_a_refusal_does(client):
    r = client.get(f"/browse/{CATALOG}", params=_filter({"genre": {"anny": ["jazz"]}}))

    # One error shape for the caller, whoever rejected it.
    assert r.json()["detail"] == {"field": "genre", "reason": "not a valid selector"}


def test_a_field_the_module_never_offered_is_422_not_an_unfiltered_list(client):
    r = client.get(f"/browse/{CATALOG}", params=_filter({"year": {"gte": 1990}}))

    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "year"


def test_an_unknown_source_is_still_404(client):
    r = client.get("/browse/kalinka:nosuch:catalog:albums")
    assert r.status_code == 404


def test_a_module_failure_is_500_not_422():
    app = FastAPI()
    register_browse_routes(
        app, lambda _: _BrokenModule(), EntityId.from_string, lambda o, l: None
    )
    r = TestClient(app, raise_server_exceptions=False).get(f"/browse/{CATALOG}")
    assert r.status_code == 500


def test_values_endpoint_serves_one_fields_vocabulary(client, module):
    r = client.get(f"/browse/{CATALOG}/filter/genre/values", params={"limit": 5, "q": "ja"})

    assert r.status_code == 200
    assert r.json()["items"] == [{"id": "jazz", "name": "Jazz", "count": 3}]
    catalog_id, field, _, limit, q = module.asked[0]
    assert (catalog_id.id, field, limit, q) == ("albums", "genre", 5, "ja")


def test_values_for_anything_but_a_catalog_is_422_before_the_module(client, module):
    r = client.get("/browse/kalinka:testsource:track:t1/filter/genre/values")

    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "id"
    assert module.asked == []


def test_values_for_a_field_that_was_never_offered_is_422(client):
    r = client.get(f"/browse/{CATALOG}/filter/year/values")

    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "year"


def test_the_root_pages_as_asked(client):
    r = client.get("/browse", params={"offset": 10, "limit": 5})

    assert r.status_code == 200
    assert client.listed_roots == [(10, 5)]


def test_the_root_refuses_a_negative_page(client):
    assert client.get("/browse", params={"offset": -1}).status_code == 422
    assert client.get("/browse", params={"limit": -1}).status_code == 422
    assert client.listed_roots == []
