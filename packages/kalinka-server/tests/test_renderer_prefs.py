"""RendererPreferences: what survives a restart, and what a bad file does."""

import json

from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_registry import RendererRegistry


def test_memory_only_store_writes_nothing(tmp_path):
    prefs = RendererPreferences()
    prefs.set_selected("rid-1")
    prefs.set_volume_control("rid-1", "musiccast")
    assert prefs.selected_renderer_id == "rid-1"
    assert list(tmp_path.iterdir()) == []


def test_values_survive_a_reload(tmp_path):
    path = str(tmp_path / "renderers.json")
    prefs = RendererPreferences(path)
    prefs.set_selected("rid-1")
    prefs.set_volume_control("rid-2", "musiccast")

    reloaded = RendererPreferences(path)
    assert reloaded.selected_renderer_id == "rid-1"
    assert reloaded.volume_control("rid-2") == "musiccast"
    assert reloaded.volume_control("rid-1") is None


def test_the_selected_renderer_s_name_survives_a_reload(tmp_path):
    path = str(tmp_path / "renderers.json")
    RendererPreferences(path).set_selected("rid-1", "Living Room")

    reloaded = RendererPreferences(path)
    assert reloaded.selected_renderer_id == "rid-1"
    assert reloaded.selected_renderer_name == "Living Room"

    reloaded.set_selected(None, "Living Room")
    assert RendererPreferences(path).selected_renderer_name is None


def test_clearing_a_mapping_drops_the_entry(tmp_path):
    path = str(tmp_path / "renderers.json")
    prefs = RendererPreferences(path)
    prefs.set_volume_control("rid-1", "musiccast")
    prefs.set_volume_control("rid-1", None)

    assert RendererPreferences(path).volume_control("rid-1") is None
    with open(path) as f:
        assert json.load(f)["renderers"] == {}


def test_clearing_an_unknown_renderer_creates_no_entry(tmp_path):
    path = str(tmp_path / "renderers.json")
    prefs = RendererPreferences(path)
    prefs.set_volume_control("rid-unknown", None)
    prefs.set_selected("rid-1")

    with open(path) as f:
        assert json.load(f)["renderers"] == {}


def test_unreadable_file_starts_empty_instead_of_raising(tmp_path):
    path = tmp_path / "renderers.json"
    path.write_text("{ not json")
    prefs = RendererPreferences(str(path))
    assert prefs.selected_renderer_id is None
    # Still usable: the next write replaces the junk.
    prefs.set_selected("rid-1")
    assert RendererPreferences(str(path)).selected_renderer_id == "rid-1"


def test_directory_is_created_on_first_write(tmp_path):
    path = str(tmp_path / "nested" / "renderers.json")
    RendererPreferences(path).set_selected("rid-1")
    assert RendererPreferences(path).selected_renderer_id == "rid-1"


async def test_registry_selection_persists(tmp_path):
    path = str(tmp_path / "renderers.json")
    registry = RendererRegistry(prefs=RendererPreferences(path))
    registry.select("rid-1")

    revived = RendererRegistry(prefs=RendererPreferences(path))
    assert revived.selected_id == "rid-1"
