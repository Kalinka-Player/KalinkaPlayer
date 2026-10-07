"""A folder travels as a catalog whose preview type picks the file browser."""

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    Catalog,
    EntityId,
    EntityType,
    Preview,
    PreviewType,
)
from kalinka_plugin_sdk.inputmodule import InputModule


def test_a_folder_endpoint_in_the_url_safe_alphabet_round_trips():
    raw = "kalinka:localfiles:catalog:folder.L211c2ljL0Fi-_Q"
    entity_id = EntityId.from_string(raw)
    assert entity_id.type == EntityType.CATALOG
    assert entity_id.id == "folder.L211c2ljL0Fi-_Q"
    assert entity_id.to_string == raw


def test_the_folder_preview_type_survives_serialisation():
    entity_id = EntityId(id="folder.L211c2lj", type=EntityType.CATALOG, source="x")
    item = BrowseItem(
        id=entity_id,
        name="music",
        can_browse=True,
        catalog=Catalog(
            id=entity_id, title="music", preview_config=Preview(type=PreviewType.FOLDER)
        ),
    )
    parsed = BrowseItem.model_validate(item.model_dump())
    assert parsed.catalog.preview_config.type is PreviewType.FOLDER
    assert PreviewType("folder") is PreviewType.FOLDER


async def test_a_module_leaves_what_adding_takes_to_the_listing_by_default():
    class _Module(InputModule):
        pass

    folder = EntityId(id="folder.L211c2lj", type=EntityType.CATALOG, source="x")

    assert await _Module().tracks_to_add(folder, 10) is None
