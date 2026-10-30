"""Packaged icons are available without enabling or calling a plugin."""

from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from kalinka_plugin_sdk import ModuleConfig, ModuleIconFiles
from kalinka_plugin_upnp.config_model import UpnpConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation
from kalinka_server.module_artwork import module_icons, register_module_icon_routes


@pytest.fixture
def artwork(tmp_path):
    small = tmp_path / "small.svg"
    small.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,26,14"><path d="M1 1h24v12H1z"/></svg>'
    )
    standard = tmp_path / "standard.png"
    Image.new("RGBA", (256, 256), "black").save(standard)

    class Config(ModuleConfig):
        __module_icon__: ClassVar[str] = "music_note_outlined"
        __module_icon_files__: ClassVar[ModuleIconFiles] = ModuleIconFiles(
            small, standard, True, min_height=21
        )

    config = Config(name="example", enabled=False)
    app = FastAPI()
    register_module_icon_routes(app, lambda: [config])
    with TestClient(app) as client:
        yield config, client


def test_disabled_plugin_serves_both_variants_and_their_dimensions(artwork):
    config, client = artwork
    icons = module_icons(config)
    assert icons.monochrome is True
    assert icons.small.aspect_ratio == pytest.approx(26 / 14)
    assert icons.standard.aspect_ratio == 1
    for icon, path in [
        (icons.small, config.__module_icon_files__.small),
        (icons.standard, config.__module_icon_files__.standard),
    ]:
        assert icon.min_height == 21
        response = client.get(icon.url)
        assert response.status_code == 200
        assert response.content == path.read_bytes()
        assert response.headers["content-type"] == icon.mime_type
        assert "immutable" in response.headers["cache-control"]
        assert "sandbox" in response.headers["content-security-policy"]


def test_conditional_get_and_head_do_not_resend_the_image(artwork):
    config, client = artwork
    url = module_icons(config).small.url
    original = client.get(url)
    head = client.head(url)
    unchanged = client.get(
        url, headers={"If-None-Match": f'W/{original.headers["etag"]}'}
    )
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(original.content))
    assert unchanged.status_code == 304
    assert unchanged.content == b""


def test_cached_metadata_and_downloads_do_not_read_files_again(artwork, monkeypatch):
    config, client = artwork
    icons = module_icons(config)

    def unavailable(*args, **kwargs):
        raise AssertionError("Icon reread")

    monkeypatch.setattr(Path, "open", unavailable)
    assert module_icons(config) == icons
    assert client.get(icons.small.url).status_code == 200


def test_unknown_icons_cannot_read_other_server_files(artwork):
    _, client = artwork
    assert client.get("/server/module-icons/unknown").status_code == 404
    assert client.get("/server/module-icons/..%2F..%2Fetc%2Fpasswd").status_code == 404


@pytest.mark.parametrize(
    "data", [b"not SVG", b"x" * (256 * 1024 + 1), b"<!DOCTYPE svg><svg/>"]
)
def test_bad_artwork_keeps_the_module_usable(artwork, data):
    config, _ = artwork
    config.__module_icon_files__.small.write_bytes(data)
    assert module_icons(config) is None


@pytest.mark.parametrize("corruption", ["bad_checksum", "incomplete_checksum"])
def test_corrupt_png_keeps_disabled_plugin_settings_available(artwork, corruption):
    config, client = artwork
    path = config.__module_icon_files__.standard
    data = bytearray(path.read_bytes())
    idat = data.index(b"IDAT")
    checksum = idat + 4 + int.from_bytes(data[idat - 4 : idat], "big")
    if corruption == "bad_checksum":
        data[checksum] ^= 0xFF
    else:
        del data[checksum + 2 :]
    path.write_bytes(data)
    with Image.open(path) as image:
        with pytest.raises(SyntaxError, match="checksum"):
            image.verify()

    schema = build_presentation(KalinkaConfig(), {config.name: config}, {})
    card = next(m for page in schema.pages for m in page.modules if m.id == config.name)

    assert card.icons is None
    assert card.icon == "music_note_outlined"
    assert module_icons(config) is None
    assert client.get("/server/module-icons/unknown").status_code == 404


def test_upnp_ships_a_wide_attribution_mark_and_a_square_standard_icon():
    icons = module_icons(UpnpConfig())
    assert icons.small.mime_type == icons.standard.mime_type == "image/svg+xml"
    assert icons.small.aspect_ratio > 1.8
    assert icons.standard.aspect_ratio == 1
    assert icons.small.url != icons.standard.url
