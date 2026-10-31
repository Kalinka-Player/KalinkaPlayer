import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk import (
    ModuleConfig,
    ModuleDescriptor,
    ModuleHealthState,
    ModuleIcon,
    ModuleIcons,
    ModulesChangedEvent,
)


def _svg():
    return ModuleIcon(url="/resource/example-mark.svg", mime_type="image/svg+xml")


@pytest.mark.parametrize("missing", ["small", "standard"])
def test_artwork_requires_both_variants(missing):
    variants = {"small": _svg(), "standard": _svg()}
    del variants[missing]

    with pytest.raises(ValidationError) as error:
        ModuleIcons(**variants)

    assert error.value.errors()[0]["loc"] == (missing,)


def test_one_svg_can_serve_both_sizes():
    icons = ModuleIcons(small=_svg(), standard=_svg(), monochrome=True)
    module = ModuleDescriptor(
        name="example",
        title="Example",
        state=ModuleHealthState.READY,
        icon="music_note_outlined",
        icons=icons,
    )
    event = ModulesChangedEvent(input_modules=[module], devices=[])

    restored = ModulesChangedEvent.model_validate_json(event.model_dump_json())

    assert restored.input_modules[0] == module
    assert restored.input_modules[0].icons.monochrome is True
    assert restored.input_modules[0].icon == "music_note_outlined"


def test_png_sizes_can_have_different_urls_and_preserve_their_colors():
    icons = ModuleIcons(
        small=ModuleIcon(url="https://example.com/icon-64.png", mime_type="image/png"),
        standard=ModuleIcon(
            url="https://example.com/icon-256.png", mime_type="image/png"
        ),
    )

    assert icons.small.url != icons.standard.url
    assert icons.monochrome is False


def test_older_module_metadata_and_configuration_need_no_artwork():
    module = ModuleDescriptor.model_validate(
        {"name": "example", "title": "Example", "state": "ready"}
    )

    assert module.icons is None
    assert ModuleConfig.__module_icons__ is None
    assert "__module_icons__" not in ModuleConfig.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "icon.svg",
        "//example.com/icon.svg",
        "file:///tmp/icon.svg",
        "data:image/svg+xml,<svg/>",
        "javascript:alert(1)",
        "https://",
        "https://user:password@example.com/icon.svg",
        "/\\example.com/icon.svg",
    ],
)
def test_icons_require_public_http_urls_or_server_rooted_paths(url):
    with pytest.raises(ValidationError):
        ModuleIcon(url=url, mime_type="image/svg+xml")


@pytest.mark.parametrize("mime_type", ["text/html", "image/jpeg", "image/gif"])
def test_only_svg_and_png_are_supported(mime_type):
    with pytest.raises(ValidationError):
        ModuleIcon(url="/resource/icon", mime_type=mime_type)


@pytest.mark.parametrize("height", [-1, 65, float("nan"), float("inf")])
def test_minimum_height_must_be_bounded_and_finite(height):
    with pytest.raises(ValidationError):
        ModuleIcon(url="/resource/icon", mime_type="image/svg+xml", min_height=height)
