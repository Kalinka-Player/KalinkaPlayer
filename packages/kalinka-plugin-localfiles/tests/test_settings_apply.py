"""A switch that turns on a feature whose packages install at the next
start asks for that restart; the rest applies as it is saved."""

from kalinka_plugin_localfiles.config_model import (
    AiSearchConfig,
    EnricherConfig,
    ProceduralArtworkConfig,
)


def _apply(model, name):
    return (model.model_fields[name].json_schema_extra or {}).get("apply")


def test_ai_search_and_generated_art_ask_for_a_restart():
    assert _apply(AiSearchConfig, "enabled") == "restart_required"
    assert _apply(ProceduralArtworkConfig, "enabled") == "restart_required"


def test_the_enricher_switch_applies_as_saved():
    assert _apply(EnricherConfig, "enabled") is None
