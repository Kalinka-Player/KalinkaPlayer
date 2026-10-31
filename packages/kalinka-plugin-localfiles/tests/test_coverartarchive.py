"""Cover Art Archive plugin: real covers for MB-matched albums no other
source has art for — the Deezer-less free/CC releases. It treats a
procedurally generated placeholder as absence so a later pass upgrades it,
and it never speaks for albums without a MusicBrainz id.
"""

from __future__ import annotations

import io
import os

import httpx
import pytest
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.enricher.coverartarchive_plugin import (
    CoverArtArchivePlugin,
)
from kalinka_plugin_localfiles.enricher.enricher_plugin import (
    TransientEnrichmentError,
)


class _FakeDb:
    """Only what the plugin asks of the database: a release's group, if
    known, and the album as it stands when its cover is written."""

    def __init__(self, rg_id=None):
        self.rg_id = rg_id
        self.asked_for: list[str] = []
        self.albums: dict[str, dict] = {}

    async def get_album_by_id(self, album_id):
        return self.albums.get(album_id, {"id": album_id})

    async def set_album_image(self, album_id, generated):
        self.albums[album_id] = {
            "id": album_id, "image_url": album_id, "image_generated": int(generated)
        }

    async def get_release_group_for_release(self, release_id):
        self.asked_for.append(release_id)
        return self.rg_id


def _plugin(tmp_path, rg_id=None) -> CoverArtArchivePlugin:
    config = LocalFilesConfig(
        db_path=str(tmp_path / "db.sqlite"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return CoverArtArchivePlugin(config, db_manager=_FakeDb(rg_id))


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 800), (200, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content


class FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.requested: list[str] = []

    async def get(self, url):
        self.requested.append(url)
        if self._error is not None:
            raise self._error
        return self._response


class RoutedClient:
    """Answers per URL substring, so a release 404 and a group 200 coexist."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requested: list[str] = []

    async def get(self, url):
        self.requested.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(404)


ALBUM = {"id": "album_1", "title": "Autumn prelude", "mbid": "mbid-1"}


class TestScope:
    def test_albums_only(self, tmp_path):
        p = _plugin(tmp_path)
        assert p.can_enrich_album()
        assert not p.can_enrich_artist()
        assert not p.can_enrich_track()


@pytest.mark.asyncio
async def test_fetches_and_saves_cover_for_matched_album(tmp_path):
    p = _plugin(tmp_path)
    p.async_client = FakeClient(FakeResponse(200, _jpeg()))

    result = await p.enrich_album(dict(ALBUM))

    assert result == {"updates": {"image_url": "album_1", "image_generated": 0}}
    assert "mbid-1" in p.async_client.requested[0]
    for suffix in ("thumbnail", "small", "large"):
        assert os.path.exists(tmp_path / "artwork" / "album" / f"album_1_{suffix}.jpg")


@pytest.mark.asyncio
async def test_real_cover_is_left_alone_but_placeholder_is_upgraded(tmp_path):
    p = _plugin(tmp_path)
    p.async_client = FakeClient(FakeResponse(200, _jpeg()))

    real = dict(ALBUM, image_url="album_1")
    assert await p.enrich_album(real) is None
    assert p.async_client.requested == []

    generated = dict(ALBUM, image_url="album_1", image_generated=1)
    result = await p.enrich_album(generated)
    assert result is not None and result["updates"]["image_generated"] == 0


@pytest.mark.asyncio
async def test_album_without_mbid_is_skipped(tmp_path):
    p = _plugin(tmp_path)
    p.async_client = FakeClient(FakeResponse(200, _jpeg()))

    assert await p.enrich_album({"id": "album_1", "title": "No match"}) is None
    assert p.async_client.requested == []


@pytest.mark.asyncio
async def test_archive_without_art_is_a_quiet_no(tmp_path):
    """404 means the archive has nothing — a verdict, not an outage."""
    p = _plugin(tmp_path)
    p.async_client = FakeClient(FakeResponse(404))

    assert await p.enrich_album(dict(ALBUM)) is None


@pytest.mark.asyncio
async def test_unreachable_archive_is_transient(tmp_path):
    """Network failures defer the row instead of ruling on it, and feed the
    per-service stand-down like every other source."""
    for failure in (
        FakeClient(error=httpx.ConnectError("boom")),
        FakeClient(FakeResponse(503)),
        FakeClient(FakeResponse(429)),
    ):
        p = _plugin(tmp_path)
        p.async_client = failure
        with pytest.raises(TransientEnrichmentError):
            await p.enrich_album(dict(ALBUM))


@pytest.mark.asyncio
async def test_undecodable_image_returns_no_update(tmp_path):
    p = _plugin(tmp_path)
    p.async_client = FakeClient(FakeResponse(200, b"not an image"))

    assert await p.enrich_album(dict(ALBUM)) is None


@pytest.mark.asyncio
async def test_a_pressing_without_art_falls_back_to_its_release_group(tmp_path):
    """MusicBrainz often matches an obscure pressing of a well-photographed
    sleeve. The cover belongs to the artwork, so the group answers for it."""
    p = _plugin(tmp_path, rg_id="rg-9")
    p.async_client = RoutedClient(
        {"/release/": FakeResponse(404), "/release-group/": FakeResponse(200, _jpeg())}
    )

    result = await p.enrich_album(dict(ALBUM))

    assert result == {"updates": {"image_url": "album_1", "image_generated": 0}}
    assert p.async_client.requested == [
        "https://coverartarchive.org/release/mbid-1/front-1200",
        "https://coverartarchive.org/release-group/rg-9/front-1200",
    ]
    assert p.db_manager.asked_for == ["mbid-1"]


@pytest.mark.asyncio
async def test_the_group_is_not_consulted_when_the_release_has_art(tmp_path):
    p = _plugin(tmp_path, rg_id="rg-9")
    p.async_client = RoutedClient({"/release/": FakeResponse(200, _jpeg())})

    assert await p.enrich_album(dict(ALBUM)) is not None
    assert p.db_manager.asked_for == []


@pytest.mark.asyncio
async def test_no_art_anywhere_stays_a_quiet_no(tmp_path):
    p = _plugin(tmp_path, rg_id="rg-9")
    p.async_client = RoutedClient({})  # every URL 404s

    assert await p.enrich_album(dict(ALBUM)) is None
    assert len(p.async_client.requested) == 2


@pytest.mark.asyncio
async def test_an_album_whose_release_group_is_unknown_stops_at_the_release(tmp_path):
    """A release id no candidate row mentions leaves nothing to fall back to."""
    p = _plugin(tmp_path, rg_id=None)
    p.async_client = RoutedClient({})

    assert await p.enrich_album(dict(ALBUM)) is None
    assert p.async_client.requested == [
        "https://coverartarchive.org/release/mbid-1/front-1200"
    ]
