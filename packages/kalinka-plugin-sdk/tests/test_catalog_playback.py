"""Catalog activation is opt-in; metadata survives the wire format."""

from kalinka_plugin_sdk.datamodel import BrowseItem, Catalog, EntityId


def test_catalogs_keep_contextual_playback_by_default():
    catalog = Catalog.model_validate({"id": "kalinka:x:catalog:tracks", "title": "Tracks"})
    assert catalog.play_individually is False


def test_individual_playback_survives_browse_item_json():
    entity_id = EntityId.from_string("kalinka:webradio:catalog:popular")
    item = BrowseItem(
        id=entity_id,
        name="Popular",
        can_browse=True,
        catalog=Catalog(id=entity_id, title="Popular", play_individually=True),
    )
    restored = BrowseItem.model_validate_json(item.model_dump_json())
    assert restored.catalog.play_individually is True
    assert restored.catalog.live is False
