"""Mood search runs on packages that install at the next start, so its
switch asks for that restart; the rest applies as it is saved."""

from kalinka_plugin_jamendo.config_model import JamendoConfig


def _apply(name):
    return (JamendoConfig.model_fields[name].json_schema_extra or {}).get("apply")


def test_mood_search_asks_for_a_restart():
    assert _apply("ai_search_enabled") == "restart_required"


def test_the_index_path_applies_as_saved():
    assert _apply("ai_index_path") is None
