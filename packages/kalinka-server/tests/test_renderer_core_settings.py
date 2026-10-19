"""The Core's own settings for a renderer, answered with the renderer's."""

import pytest

from kalinka_server.renderer_config import _field_to_dict
from kalinka_server.renderer_core_settings import (
    DEVICE_MODULE_PATH,
    REPLAYGAIN_PATH,
    RENDERER_ITSELF,
    SECTION_PATH,
    CoreRendererSettings,
)
from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_proto import renderer_pb2 as pb

RENDERER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Resyncs:
    def __init__(self):
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def prefs():
    return RendererPreferences()


@pytest.fixture
def resync():
    return Resyncs()


@pytest.fixture
def settings(prefs, resync):
    return CoreRendererSettings(
        prefs,
        lambda: [("musiccast", "Yamaha MusicCast"), ("dummydevice", "Dummy")],
        resync,
    )


def _field(settings, renderer_id=RENDERER) -> dict:
    section = settings.section(renderer_id)
    assert section["path"] == SECTION_PATH
    assert len(section["fields"]) == 1
    return section["fields"][0]


def test_every_loaded_module_is_offered_alongside_the_renderer(settings):
    field = _field(settings)

    assert [option["value"] for option in field["options"]] == [
        RENDERER_ITSELF,
        "musiccast",
        "dummydevice",
    ]
    assert field["options"][1]["label"] == "Yamaha MusicCast"
    assert field["value"] == RENDERER_ITSELF, "unmapped by default"


async def test_mapping_a_renderer_is_remembered_and_resynced(settings, prefs, resync):
    outcomes = await settings.apply(RENDERER, {DEVICE_MODULE_PATH: "musiccast"})

    assert outcomes == [
        {
            "path": DEVICE_MODULE_PATH,
            "applied": True,
            "value": "musiccast",
            "error": "",
        }
    ]
    assert prefs.volume_control(RENDERER) == "musiccast"
    assert resync.count == 1, "whoever owns the output changed"
    assert _field(settings)["value"] == "musiccast"


async def test_the_mapping_is_per_renderer(settings, prefs):
    await settings.apply(RENDERER, {DEVICE_MODULE_PATH: "musiccast"})

    assert prefs.volume_control(OTHER) is None
    assert _field(settings, OTHER)["value"] == RENDERER_ITSELF


async def test_clearing_it_hands_control_back_to_the_renderer(settings, prefs, resync):
    await settings.apply(RENDERER, {DEVICE_MODULE_PATH: "musiccast"})

    await settings.apply(RENDERER, {DEVICE_MODULE_PATH: RENDERER_ITSELF})

    assert prefs.volume_control(RENDERER) is None
    assert resync.count == 2


async def test_a_module_that_cannot_take_the_output_is_refused(settings, prefs, resync):
    outcomes = await settings.apply(RENDERER, {DEVICE_MODULE_PATH: "nosuch"})

    assert outcomes[0]["applied"] is False
    assert "not an enabled device module" in outcomes[0]["error"]
    assert prefs.volume_control(RENDERER) is None
    assert resync.count == 0


async def test_a_path_that_is_not_ours_is_refused_rather_than_guessed(settings):
    outcomes = await settings.apply(RENDERER, {"output.device": "default"})

    assert outcomes[0]["applied"] is False
    assert outcomes[0]["error"] == "no such setting"


def test_a_mapping_whose_module_is_gone_stays_selectable(prefs, resync):
    prefs.set_volume_control(RENDERER, "musiccast")
    settings = CoreRendererSettings(prefs, lambda: [], resync)

    field = _field(settings)

    assert field["value"] == "musiccast"
    assert [option["value"] for option in field["options"]] == [
        RENDERER_ITSELF,
        "musiccast",
    ]
    assert "unavailable" in field["options"][1]["label"]


def test_with_nothing_to_map_to_it_still_says_who_has_the_output(prefs, resync):
    settings = CoreRendererSettings(prefs, lambda: [], resync)

    field = _field(settings)

    assert field["read_only"] is True
    assert field["value"] == "This renderer", "read-only is shown as its value"


def test_the_core_field_is_shaped_like_one_of_the_renderers_own(settings):
    field = _field(settings)

    assert set(field) == set(_field_to_dict(pb.ConfigField())), "one page, one shape"
    assert field["importance"] == "simple", "it is the mapping a user comes here for"


def test_the_core_section_rides_the_renderers_payload(settings):
    snapshot = {"config_version": "abc", "sections": [{"path": "output"}]}

    merged = settings.merge_into(snapshot, RENDERER)

    assert [section["path"] for section in merged["sections"]] == [
        "output",
        SECTION_PATH,
    ]
    assert merged["config_version"].startswith("abc.")


def test_the_version_changes_with_the_modules_on_offer(prefs, resync):
    one = CoreRendererSettings(prefs, lambda: [("musiccast", "M")], resync)
    two = CoreRendererSettings(
        prefs, lambda: [("musiccast", "M"), ("dummydevice", "D")], resync
    )

    assert one.fingerprint() != two.fingerprint()


def test_writes_are_split_from_the_renderers_own(settings):
    ours, theirs = settings.split(
        {DEVICE_MODULE_PATH: "musiccast", "output.device": "default"}
    )

    assert ours == {DEVICE_MODULE_PATH: "musiccast"}
    assert theirs == {"output.device": "default"}


def _config(mode):
    return {
        "sections": [
            {
                "path": "output",
                "fields": [
                    {"path": "output.volume_mode", "value": mode},
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    "mode,available",
    [("auto", True), ("fixed", False), ("hardware", False), ("software", False)],
)
def test_replaygain_choices_and_volume_requirement(settings, prefs, mode, available):
    merged = settings.merge_into(_config(mode), RENDERER)
    field = merged["sections"][-1]["fields"][-1]
    assert field["path"] == REPLAYGAIN_PATH
    assert field["value"] == "disabled"
    assert [o["value"] for o in field["options"]] == ["disabled", "album", "track"]
    assert field["read_only"] is not available
    prefs.set_volume_control(RENDERER, "musiccast")
    assert (
        settings.merge_into(_config(mode), RENDERER)["sections"][-1]["fields"][-1][
            "read_only"
        ]
        is False
    )


async def test_replaygain_is_validated_persisted_per_renderer_and_resynced(
    tmp_path, resync
):
    path = str(tmp_path / "renderers.json")
    prefs = RendererPreferences(path)

    async def get_config(renderer_id):
        return _config("auto")

    settings = CoreRendererSettings(prefs, lambda: [], resync, get_config)
    result = await settings.apply(RENDERER, {REPLAYGAIN_PATH: "album"})
    assert result[0]["applied"] is True
    assert RendererPreferences(path).replaygain(RENDERER) == "album"
    assert prefs.replaygain(OTHER) == "disabled"
    assert resync.count == 1
    result = await settings.apply(RENDERER, {REPLAYGAIN_PATH: "invalid"})
    assert result[0]["applied"] is False
    assert prefs.replaygain(RENDERER) == "album"


async def test_enabling_replaygain_with_fixed_volume_is_rejected(prefs):
    async def get_config(renderer_id):
        return _config("fixed")

    settings = CoreRendererSettings(prefs, lambda: [], get_config=get_config)
    result = await settings.apply(RENDERER, {REPLAYGAIN_PATH: "track"})
    assert result[0]["applied"] is False
    assert "Automatic" in result[0]["error"]
    result = await settings.apply(RENDERER, {REPLAYGAIN_PATH: "disabled"})
    assert result[0]["applied"] is True


async def test_combined_device_and_replaygain_write_does_not_depend_on_field_order(
    settings, prefs
):
    result = await settings.apply(
        RENDERER,
        {
            REPLAYGAIN_PATH: "track",
            DEVICE_MODULE_PATH: "musiccast",
        },
    )
    assert all(outcome["applied"] for outcome in result)
    assert prefs.replaygain(RENDERER) == "track"


async def test_renderer_settings_api_applies_volume_mode_before_replaygain(
    monkeypatch, tmp_path
):
    from kalinka_server import server
    from kalinka_server.config_model import KalinkaConfig
    from kalinka_server.renderer_config import RendererConfigService
    from tests.app_harness import isolate_app, running
    from tests.sim_renderer import SimRenderer

    isolate_app(monkeypatch, tmp_path)
    mode = "fixed"

    async def get_config(self, renderer_id):
        return _config(mode)

    async def update_config(self, renderer_id, changes):
        nonlocal mode
        mode = changes["output.volume_mode"]
        return {
            "config_version": "1",
            "effect": "interrupts_playback",
            "outcomes": [
                {
                    "path": "output.volume_mode",
                    "applied": True,
                    "value": mode,
                    "error": "",
                },
            ],
        }

    monkeypatch.setattr(RendererConfigService, "get", get_config)
    monkeypatch.setattr(RendererConfigService, "update", update_config)
    app = await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})
    sim = SimRenderer(app.state.renderer_registry, app.state.renderer_sessions)
    sim.connect()
    url = f"/renderer/{sim.RENDERER_ID}/config"
    async with running(app) as client:
        response = await client.put(url, json={"changes": {REPLAYGAIN_PATH: "track"}})
        assert response.json()["outcomes"][0]["applied"] is False
        response = await client.put(
            url,
            json={
                "changes": {
                    REPLAYGAIN_PATH: "album",
                    "output.volume_mode": "auto",
                }
            },
        )
        assert response.status_code == 200
        assert all(o["applied"] for o in response.json()["outcomes"])
        fields = (await client.get(url)).json()["sections"][-1]["fields"]
        replaygain = next(f for f in fields if f["path"] == REPLAYGAIN_PATH)
        assert replaygain["value"] == "album"
        assert replaygain["read_only"] is False
