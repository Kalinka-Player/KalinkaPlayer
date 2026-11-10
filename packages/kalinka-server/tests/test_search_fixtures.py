"""Recorded searches replayed through the ranker: a ranked source's answer is
shown as it gave it.

Each fixture in ``search_fixtures`` is one source's hits of every kind for one
query, as its plugin returned them; TIDAL's are recorded by the plugin's
``scripts/capture_search.py``.
"""

import json
from pathlib import Path

import pytest

from kalinka_plugin_sdk.datamodel import BrowseItem
from kalinka_server.name_matches import SourceHits, rank

FIXTURES = sorted((Path(__file__).parent / "search_fixtures").glob("*.json"))
KIND_ORDER = ("artist", "album", "playlist", "track")


def _replayed(path):
    fixture = json.loads(path.read_text())
    kinds = {
        kind: [BrowseItem.model_validate(hit) for hit in hits]
        for kind, hits in fixture["kinds"].items()
    }
    source = SourceHits(kinds=list(kinds.values()), ranked=fixture["ranked"])
    return kinds, rank(fixture["query"], [source])


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_every_kind_keeps_the_sources_order_and_every_hit(path):
    kinds, ranked = _replayed(path)
    for kind, hits in kinds.items():
        shown = [item.id for item in ranked if item.id.type.value == kind]
        assert shown == [item.id for item in hits], kind


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_all_opens_with_each_kinds_first_hit(path):
    kinds, ranked = _replayed(path)
    leads = [kinds[kind][0].id for kind in KIND_ORDER if kinds.get(kind)]
    assert [item.id for item in ranked[: len(leads)]] == leads
