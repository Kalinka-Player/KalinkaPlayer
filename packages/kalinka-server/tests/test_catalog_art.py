"""Tests for the browse-item art renderers and the generation service."""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from kalinka_plugin_sdk.datamodel import (
    Album,
    BrowseItem,
    BrowseItemList,
    Catalog,
    CatalogRole,
    CoverImage,
    EntityId,
    EntityType,
    Owner,
    Playlist,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.inputmodule import ContentInfo

from kalinka_server import catalog_art_render as render
from kalinka_server.catalog_art_service import (
    _item_image_path,
    ART_URL_PREFIX,
    FAIL_RETRY_SECONDS,
    MAX_COVER_BYTES,
    PARTIAL_RETRY_SECONDS,
    REFRESH_SECONDS,
    CatalogArtService,
)

# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------


def _solid_cover(color, size=120):
    return Image.new("RGB", (size, size), color)


def test_render_card_with_covers_is_deterministic():
    covers = [_solid_cover((200, 40, 40)), _solid_cover((40, 200, 40))]
    a = render.render_catalog_art(covers, [], "seed:x", width=320, height=180)
    b = render.render_catalog_art(covers, [], "seed:x", width=320, height=180)
    assert a.size == (320, 180)
    assert a.mode == "RGB"  # opaque, full-bleed tile
    assert a.tobytes() == b.tobytes()


def test_render_coverless_tile_varies_by_seed():
    # Grain varies by catalog while the brand palette stays fixed.
    rock = render.render_catalog_art([], [], "cat:rock", width=320, height=180)
    jazz = render.render_catalog_art([], [], "cat:jazz", width=320, height=180)
    assert rock.mode == "RGB"
    assert rock.tobytes() != jazz.tobytes()


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("square", [False, True])
def test_collage_keeps_each_album_visible_and_limits_it_to_three(count, square):
    import numpy as np

    colors = [(220, 25, 25), (25, 220, 25), (25, 25, 220), (220, 220, 25)]
    covers = [_solid_cover(color) for color in colors]

    def compose(images):
        if square:
            return render.render_catalog_thumbnail(images, "collage", side=240)
        return render.render_catalog_art(images, [], "collage", width=600, height=200)

    image = compose(covers[:count])
    pixels = np.asarray(image, dtype=np.int16)
    for color in colors[:count]:
        visible = np.max(np.abs(pixels - color), axis=2) < 10
        assert visible.sum() > image.width * image.height * 0.025
    if count == 3:
        assert image.tobytes() == compose(covers).tobytes()
    if not square:
        text_column = (0, 0, round(image.width * 0.4), image.height)
        assert (
            image.crop(text_column).tobytes() == compose([]).crop(text_column).tobytes()
        )


def test_album_colors_do_not_recolor_the_kalinka_backdrop():
    red = render.render_catalog_art([_solid_cover("red")], [], "brand")
    blue = render.render_catalog_art([_solid_cover("blue")], [], "brand")
    margin = (1140, 0, 1200, 400)
    assert red.crop(margin).tobytes() == blue.crop(margin).tobytes()


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_thumbnail_is_the_card_art_square(count):
    covers = [_solid_cover((60 * i, 200 - 40 * i, 80)) for i in range(count)]
    a = render.render_catalog_thumbnail(covers, "seed:x", side=96)
    b = render.render_catalog_thumbnail(covers, "seed:x", side=96)
    assert a.size == (96, 96)
    assert a.mode == "RGB"
    assert a.tobytes() == b.tobytes()
    if count:
        bare = render.render_catalog_thumbnail([], "seed:x", side=96)
        assert a.tobytes() != bare.tobytes()


def test_root_titles_have_distinct_color_families():
    titles = ["Home", "Explore", "Genres", "Moods", "My Mixes", "My Collection"]
    colors = {
        render._background_colors(render.background_text_key([title], "tidal"))[0]
        for title in titles
    }
    assert len(colors) >= 5


def test_encode_jpeg_roundtrips():
    img = render.render_catalog_art(
        [_solid_cover((80, 80, 200))], [], "s", width=160, height=90
    )
    data = render.encode_jpeg(img)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    Image.open(io.BytesIO(data)).verify()


def test_fingerprint_changes_with_style_version(monkeypatch):
    fp1 = render.content_fingerprint([b"a"], ["N"], "seed")
    monkeypatch.setattr(render, "STYLE_VERSION", render.STYLE_VERSION + 1)
    fp2 = render.content_fingerprint([b"a"], ["N"], "seed")
    assert fp1 != fp2


def test_fingerprint_tells_the_two_shapes_apart():
    card = render.content_fingerprint([b"a"], [], "seed", render.ArtStyle.CARD)
    cover = render.content_fingerprint([b"a"], [], "seed", render.ArtStyle.COVER)
    assert card != cover


def test_playlist_cover_mosaics_four_albums():
    colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30)]
    cover = render.render_playlist_cover([_solid_cover(c) for c in colors], side=200)

    assert cover.size == (200, 200)
    quadrants = [(50, 50), (150, 50), (50, 150), (150, 150)]
    assert [cover.getpixel(point) for point in quadrants] == colors


def test_playlist_cover_takes_one_album_below_four():
    covers = [_solid_cover((200, 30, 30)), _solid_cover((30, 200, 30))]
    cover = render.render_playlist_cover(covers, side=200)

    assert cover.size == (200, 200)
    # The whole square is the first album — no half-filled grid.
    assert cover.getpixel((50, 50)) == cover.getpixel((150, 150)) == (200, 30, 30)


def test_playlist_cover_squares_an_oblong_album():
    cover = render.render_playlist_cover(
        [Image.new("RGB", (400, 100), (10, 20, 30))], side=120
    )
    assert cover.size == (120, 120)


# --------------------------------------------------------------------------
# Service — inline decoration
# --------------------------------------------------------------------------


def _catalog_item(local, *, image=None, preview=None):
    eid = EntityId(id=local, type=EntityType.CATALOG, source="localfiles")
    return BrowseItem(
        id=eid,
        name=local.title(),
        can_browse=True,
        catalog=Catalog(
            id=eid, title=local.title(), image=image, preview_config=preview
        ),
    )


def _album_child(local, *, with_image=True, query=""):
    eid = EntityId(id=local, type=EntityType.ALBUM, source="localfiles")
    image = (
        CoverImage(
            large=f"/resource/album/{local}_large.jpg{query}",
            small=f"/resource/album/{local}_small.jpg{query}",
            thumbnail=f"/resource/album/{local}_thumb.jpg{query}",
        )
        if with_image
        else None
    )
    return BrowseItem(
        id=eid,
        name=local,
        can_browse=True,
        album=Album(id=eid, title=local, image=image),
    )


def _track_with_cover(own=None):
    album_id = EntityId(id="a1", type=EntityType.ALBUM, source="localfiles")
    track_id = EntityId(id="t1", type=EntityType.TRACK, source="localfiles")
    album = Album(id=album_id, title="A", image=CoverImage(large="/album.jpg"))
    track = Track(id=track_id, title="T", duration=1, album=album, image=own)
    return BrowseItem(id=track_id, name="T", can_browse=False, track=track)


def test_a_track_tile_shows_the_tracks_own_cover_as_the_app_does():
    assert (
        _item_image_path(_track_with_cover(CoverImage(large="/own.jpg"))) == "/own.jpg"
    )
    assert _item_image_path(_track_with_cover()) == "/album.jpg"
    assert _item_image_path(_track_with_cover(CoverImage())) == "/album.jpg"


def _service(tmp_path, resolver=lambda eid: None, resource_resolver=None):
    """The fake plays both roles unless a test wants them apart — a source
    that browses need not be one that can serve a file."""
    return CatalogArtService(
        str(tmp_path / "catalog_art"), resolver, resource_resolver or resolver
    )


def test_decorate_enqueues_and_does_not_block(tmp_path):
    svc = _service(tmp_path)
    result = BrowseItemList(
        offset=0, limit=10, total=1, items=[_catalog_item("albums")]
    )
    svc.decorate(result)
    # No file yet → image stays empty, but a job is queued.
    assert result.items[0].catalog.image is None
    assert svc._queue.qsize() == 1


def _collection_item(local, *, track_count):
    """A collection as the collections source lists it: one id, a playlist
    payload for rows and a catalog payload for the page."""
    eid = EntityId(id=local, type=EntityType.PLAYLIST, source="collections")
    owner = Owner(
        name="You", id=EntityId(id="you", type=EntityType.USER, source="collections")
    )
    return BrowseItem(
        id=eid,
        name=local,
        can_browse=True,
        can_add=True,
        can_edit=True,
        playlist=Playlist(
            id=eid, name=local, owner=owner, description="", track_count=track_count
        ),
        catalog=Catalog(id=eid, title=local),
    )


def test_a_collection_with_tracks_carries_its_art_on_both_payloads(tmp_path):
    svc = _service(tmp_path)
    item = _collection_item("c1", track_count=3)
    svc._entries[item.id.to_string] = {"file": "abc.jpg", "next_check_at": 1e12}

    svc.decorate(BrowseItemList(offset=0, limit=10, total=1, items=[item]))

    assert item.catalog.image is not None
    assert item.catalog.image.aspect_ratio == 1.0
    assert item.playlist.image == item.catalog.image


def test_a_collection_asks_for_a_cover_and_a_catalog_for_a_card(tmp_path):
    svc = _service(tmp_path)
    items = [_collection_item("c1", track_count=3), _catalog_item("albums")]

    svc.decorate(BrowseItemList(offset=0, limit=10, total=2, items=items))

    queued = {}
    while not svc._queue.empty():
        cat_id, style, _, _ = svc._queue.get_nowait()
        queued[cat_id] = style
    assert queued == {
        items[0].id.to_string: render.ArtStyle.COVER,
        items[1].id.to_string: render.ArtStyle.CARD,
    }


def test_an_empty_collection_gets_no_art_and_asks_for_none(tmp_path):
    svc = _service(tmp_path)
    item = _collection_item("c1", track_count=0)
    svc._entries[item.id.to_string] = {"file": "abc.jpg", "next_check_at": 0.0}

    svc.decorate(BrowseItemList(offset=0, limit=10, total=1, items=[item]))

    assert item.catalog.image is None
    assert item.playlist.image is None
    assert svc._queue.empty()


def test_decorate_skips_items_with_own_image(tmp_path):
    svc = _service(tmp_path)
    img = CoverImage(large="/resource/x.jpg")
    result = BrowseItemList(
        offset=0, limit=10, total=1, items=[_catalog_item("albums", image=img)]
    )
    svc.decorate(result)
    assert svc._queue.qsize() == 0


def test_decorate_leaves_a_folder_to_its_source_and_the_client(tmp_path):
    svc = _service(tmp_path)
    item = _catalog_item("folder.L211c2lj", preview=Preview(type=PreviewType.FOLDER))
    svc._entries[item.id.to_string] = {"file": "abc.jpg", "next_check_at": 0.0}

    svc.decorate(BrowseItemList(offset=0, limit=10, total=1, items=[item]))

    assert item.catalog.image is None
    assert svc._queue.empty()


def test_a_library_of_folders_on_a_home_surface_still_gets_its_card(tmp_path):
    svc = _service(tmp_path)
    item = _catalog_item("files", preview=Preview(type=PreviewType.FOLDER))
    item.catalog.role = CatalogRole.LIBRARY

    svc.decorate(BrowseItemList(offset=0, limit=10, total=1, items=[item]))

    assert svc._queue.qsize() == 1


def test_decorate_fills_url_once_generated(tmp_path):
    svc = _service(tmp_path)
    cat_id = "kalinka:localfiles:catalog:albums"
    svc._entries[cat_id] = {
        "file": "abcdef0123456789-0a1b2c3d.jpg",
        "fingerprint": "x",
        "next_check_at": 1e18,  # far future → no re-enqueue
        "text_key": render.background_text_key(["Albums"], cat_id),
    }
    result = BrowseItemList(
        offset=0, limit=10, total=1, items=[_catalog_item("albums")]
    )
    svc.decorate(result)
    url = result.items[0].catalog.image.large
    assert url == f"{ART_URL_PREFIX}/abcdef0123456789-0a1b2c3d.jpg"
    assert (
        result.items[0].catalog.image.aspect_ratio == render.CANVAS_W / render.CANVAS_H
    )
    # A card made before thumbnails were stands in for its own.
    assert result.items[0].catalog.image.thumbnail == url
    assert svc._queue.qsize() == 0


def test_a_card_sends_its_square_thumbnail(tmp_path):
    svc = _service(tmp_path)
    cat_id = "kalinka:localfiles:catalog:albums"
    svc._entries[cat_id] = {
        "file": "abcdef0123456789-0a1b2c3d.jpg",
        "thumbnail": "abcdef0123456789-0a1b2c3d-thumb.jpg",
        "fingerprint": "x",
        "next_check_at": 1e18,
    }
    result = BrowseItemList(
        offset=0, limit=10, total=1, items=[_catalog_item("albums")]
    )
    svc.decorate(result)
    image = result.items[0].catalog.image
    assert (
        image.large == image.small == f"{ART_URL_PREFIX}/abcdef0123456789-0a1b2c3d.jpg"
    )
    assert image.thumbnail == f"{ART_URL_PREFIX}/abcdef0123456789-0a1b2c3d-thumb.jpg"


def test_art_file_rejects_bad_names(tmp_path):
    svc = _service(tmp_path)
    assert svc.art_file("../../etc/passwd") is None
    assert svc.art_file("not-a-fingerprint.jpg") is None
    assert svc.art_file("abcdef0123456789-0a1b2c3d.png") is None
    assert svc.art_file("abcdef0123456789-0a1b2c3d-thumbs.jpg") is None


# --------------------------------------------------------------------------
# Service — generation worker
# --------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, children, cover_bytes_by_resource=None):
        self._children = children
        self._covers = cover_bytes_by_resource or {}

    async def browse(self, entity_id, offset=0, limit=10, filter=None):
        return BrowseItemList(
            offset=0, limit=limit, total=len(self._children), items=self._children
        )

    async def get_resource_path(self, resource):
        return self._covers.get(resource)


def _covers_on_disk(tmp_path, *locals_, color=(200, 50, 50)):
    """Album covers a resource resolver can hand back, keyed the way the child
    items reference them."""
    covers = {}
    for local in locals_:
        path = tmp_path / f"{local}.jpg"
        path.write_bytes(render.encode_jpeg(_solid_cover(color)))
        covers[f"album/{local}_large.jpg"] = str(path)
    return covers


async def test_process_generates_a_card_with_covers(tmp_path):
    covers = _covers_on_disk(tmp_path, "a1", "a2")
    children = [_album_child("a1"), _album_child("a2")]
    module = _FakeModule(children, covers)
    svc = _service(tmp_path, resolver=lambda eid: module)

    cat_id = "kalinka:localfiles:catalog:albums"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    entry = svc._entries[cat_id]
    assert entry["file"]
    assert (svc._dir / entry["file"]).is_file()
    # Served names validate and re-fetch with unchanged inputs reuses the files.
    assert svc.art_file(entry["file"]) is not None
    thumbnail = svc.art_file(entry["thumbnail"])
    assert thumbnail is not None
    with Image.open(thumbnail) as square:
        assert square.width == square.height == render.THUMBNAIL_SIDE
    first = (entry["file"], entry["thumbnail"])
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    assert (svc._entries[cat_id]["file"], svc._entries[cat_id]["thumbnail"]) == first
    # Atomic writes must not leave temp litter behind.
    assert list(svc._dir.glob("*.tmp")) == []


async def test_a_cover_link_with_a_query_still_reads_from_disk(tmp_path):
    """A link may carry a query and still name the same file. Read as part of
    the name it matches nothing, and every local cover fails at once."""
    covers = _covers_on_disk(tmp_path, "a1", "a2", "a3")
    children = [
        _album_child(local, query=f"?v=178913192{n}")
        for n, local in enumerate(("a1", "a2", "a3"))
    ]
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule(children, covers))

    cat_id = "kalinka:localfiles:catalog:library"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    entry = svc._entries[cat_id]
    assert entry["file"]
    assert (svc._dir / entry["file"]).is_file()
    # Covers were found, so the card is finished rather than held for a retry.
    assert entry["provisional"] is False


async def test_a_collection_is_rendered_as_a_square_cover(tmp_path):
    names = ("a1", "a2", "a3", "a4")
    module = _FakeModule(
        [_album_child(local) for local in names], _covers_on_disk(tmp_path, *names)
    )
    svc = _service(tmp_path, resolver=lambda eid: module)

    cat_id = "kalinka:collections:playlist:c1"
    await svc._process(cat_id, render.ArtStyle.COVER, hint=None)

    with Image.open(svc._dir / svc._entries[cat_id]["file"]) as image:
        assert image.width == image.height
    # A cover is square already; it is its own thumbnail.
    assert svc._entries[cat_id]["thumbnail"] is None


def _track_child(album_local, track_local):
    """A track as a collection lists it, wearing its album's picture under a
    URL of its own — the shape a source hands back when it stamps the track
    into the query, as Jamendo does."""
    album_id = EntityId(id=album_local, type=EntityType.ALBUM, source="localfiles")
    track_id = EntityId(id=track_local, type=EntityType.TRACK, source="localfiles")
    return BrowseItem(
        id=track_id,
        name=track_local,
        can_browse=False,
        can_add=True,
        track=Track(
            id=track_id,
            title=track_local,
            duration=100,
            album=Album(
                id=album_id,
                title=album_local,
                image=CoverImage(
                    large=f"/resource/album/{album_local}-{track_local}.jpg"
                ),
            ),
        ),
    )


def _album_tracks(tmp_path, resources, album_local, track_locals, color):
    """Tracks of one album, each naming the same picture by its own URL, with
    the bytes written where the resource resolver will find them."""
    blob = render.encode_jpeg(_solid_cover(color))
    children = []
    for track_local in track_locals:
        path = tmp_path / f"{album_local}-{track_local}.jpg"
        path.write_bytes(blob)
        resources[f"album/{album_local}-{track_local}.jpg"] = str(path)
        children.append(_track_child(album_local, track_local))
    return children


def _cover_spy(monkeypatch):
    """Records what each mosaic was composed from."""
    composed: list[list] = []

    def _spy(covers, **kwargs):
        composed.append(list(covers))
        return render.render_playlist_cover(covers, **kwargs)

    monkeypatch.setattr(
        "kalinka_server.catalog_art_service.render_playlist_cover", _spy
    )
    return composed


async def test_a_mosaic_takes_four_distinct_albums_not_the_first_four(
    tmp_path, monkeypatch
):
    resources: dict[str, str] = {}
    children = (
        _album_tracks(tmp_path, resources, "a1", ["t1", "t2", "t3"], (200, 30, 30))
        + _album_tracks(tmp_path, resources, "a2", ["t4"], (30, 200, 30))
        + _album_tracks(tmp_path, resources, "a3", ["t5"], (30, 30, 200))
        + _album_tracks(tmp_path, resources, "a4", ["t6"], (200, 200, 30))
    )
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule(children, resources))
    composed = _cover_spy(monkeypatch)

    await svc._process(
        "kalinka:collections:playlist:c1", render.ArtStyle.COVER, hint=None
    )

    # The first three entries are one album under three URLs; the mosaic
    # reaches past them for four pictures that differ.
    assert len(composed[0]) == 4
    assert len({cover.tobytes() for cover in composed[0]}) == 4


async def test_one_album_under_many_urls_takes_the_cover_alone(tmp_path, monkeypatch):
    resources: dict[str, str] = {}
    children = _album_tracks(
        tmp_path, resources, "a1", ["t1", "t2", "t3", "t4"], (200, 30, 30)
    )
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule(children, resources))
    composed = _cover_spy(monkeypatch)

    await svc._process(
        "kalinka:collections:playlist:c1", render.ArtStyle.COVER, hint=None
    )

    # Nothing to mosaic, so the one picture gets the whole square rather than
    # being tiled four times.
    assert len(composed[0]) == 1


async def test_two_albums_wearing_one_picture_count_once(tmp_path, monkeypatch):
    resources: dict[str, str] = {}
    children = (
        _album_tracks(tmp_path, resources, "a1", ["t1"], (200, 30, 30))
        + _album_tracks(tmp_path, resources, "a2", ["t2"], (200, 30, 30))
        + _album_tracks(tmp_path, resources, "a3", ["t3"], (30, 200, 30))
        + _album_tracks(tmp_path, resources, "a4", ["t4"], (30, 30, 200))
    )
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule(children, resources))
    composed = _cover_spy(monkeypatch)

    await svc._process(
        "kalinka:collections:playlist:c1", render.ArtStyle.COVER, hint=None
    )

    # Four albums, three pictures between them: an id says two covers differ,
    # the bytes say otherwise, and the bytes win.
    assert len(composed[0]) == 3


async def test_a_collection_takes_covers_from_the_sources_that_own_them(tmp_path):
    """Collections browse but own no bytes: a child's cover belongs to whatever
    source that track came from, so it is the resource resolver — not the
    browsed source — that has to serve it."""
    owner = _FakeModule([], _covers_on_disk(tmp_path, "a1"))
    svc = _service(
        tmp_path,
        resolver=lambda eid: _FakeModule([_album_child("a1")]),
        resource_resolver=lambda eid: owner,
    )

    cat_id = "kalinka:collections:playlist:c1"
    await svc._process(cat_id, render.ArtStyle.COVER, hint=None)

    assert svc._entries[cat_id]["file"]


class _ContentModule:
    def __init__(self, blobs):
        self._blobs = blobs

    async def get_content_info(self, asset_id):
        blob = self._blobs.get(asset_id)
        if blob is None:
            return None
        return ContentInfo(
            mime_type="image/jpeg", reader=lambda: io.BytesIO(blob), size=len(blob)
        )


async def test_a_cover_served_through_content_is_asked_of_the_module_it_names(
    tmp_path,
):
    eid = EntityId(id="a1", type=EntityType.ALBUM, source="qobuz")
    child = BrowseItem(
        id=eid,
        name="a1",
        can_browse=True,
        album=Album(
            id=eid, title="a1", image=CoverImage(large="/content/qobuz/banner-a1.jpg")
        ),
    )
    owner = _ContentModule(
        {"banner-a1.jpg": render.encode_jpeg(_solid_cover((200, 50, 50)))}
    )
    svc = _service(
        tmp_path,
        resolver=lambda eid: _FakeModule([child]),
        resource_resolver=lambda source: owner if source == "qobuz" else None,
    )

    cat_id = "kalinka:collections:playlist:c1"
    await svc._process(cat_id, render.ArtStyle.COVER, hint=None)

    assert svc._entries[cat_id]["file"]


async def test_a_content_cover_link_carrying_a_query_names_the_asset_before_it(
    tmp_path,
):
    owner = _ContentModule({"a1.jpg": b"cover"})
    svc = _service(tmp_path, resource_resolver=lambda source: owner)
    item = BrowseItem(
        id=EntityId(id="a1", type=EntityType.ALBUM, source="qobuz"), name="a1"
    )

    assert await svc._fetch_cover("/content/qobuz/a1.jpg?v=2", item) == b"cover"


async def test_a_content_cover_larger_than_its_declared_size_is_refused(tmp_path):
    class _Liar(_ContentModule):
        async def get_content_info(self, asset_id):
            return ContentInfo(
                mime_type="image/jpeg",
                reader=lambda: io.BytesIO(b"x" * (MAX_COVER_BYTES + 1)),
                size=10,
            )

    svc = _service(tmp_path, resource_resolver=lambda source: _Liar({}))
    item = BrowseItem(
        id=EntityId(id="a1", type=EntityType.ALBUM, source="qobuz"), name="a1"
    )

    assert await svc._fetch_cover("/content/qobuz/a1.jpg", item) is None


async def test_a_collection_with_no_reachable_cover_ships_nothing(tmp_path):
    """A mosaic is its albums and nothing else: with none of them to hand
    there is no art to make, and the client's own tile stands in."""
    svc = _service(
        tmp_path,
        resolver=lambda eid: _FakeModule([_album_child("a1")]),
        resource_resolver=lambda eid: None,
    )

    cat_id = "kalinka:collections:playlist:c1"
    await svc._process(cat_id, render.ArtStyle.COVER, hint=None)

    assert svc._entries[cat_id].get("file") is None
    assert list(svc._dir.glob("*.jpg")) == []


async def test_process_uses_a_placeholder_when_children_are_catalogs(tmp_path):
    def _sub(local):
        eid = EntityId(id=local, type=EntityType.CATALOG, source="localfiles")
        return BrowseItem(
            id=eid, name=local, can_browse=True, catalog=Catalog(id=eid, title=local)
        )

    children = [_sub("rock"), _sub("jazz"), _sub("pop")]
    module = _FakeModule(children)
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:genres"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=PreviewContentType.CATALOG)
    assert (svc._dir / svc._entries[cat_id]["file"]).is_file()


async def test_a_page_of_folders_lends_the_card_their_covers(tmp_path, monkeypatch):
    def _folder(local):
        eid = EntityId(id=local, type=EntityType.CATALOG, source="localfiles")
        image = CoverImage(large=f"/resource/album/{local}_large.jpg")
        return BrowseItem(
            id=eid,
            name=local,
            can_browse=True,
            catalog=Catalog(
                id=eid,
                title=local,
                image=image,
                preview_config=Preview(type=PreviewType.FOLDER),
            ),
        )

    covers = {
        **_covers_on_disk(tmp_path, "rock", color=(200, 50, 50)),
        **_covers_on_disk(tmp_path, "jazz", color=(50, 50, 200)),
    }
    module = _FakeModule([_folder("rock"), _folder("jazz")], covers)
    svc = _service(tmp_path, resolver=lambda eid: module)
    composed: list[list] = []

    def _spy(covers, names, seed, **kwargs):
        composed.append(list(covers))
        return render.render_catalog_art(covers, names, seed, **kwargs)

    monkeypatch.setattr("kalinka_server.catalog_art_service.render_catalog_art", _spy)

    await svc._process(
        "kalinka:localfiles:catalog:files", render.ArtStyle.CARD, hint=None
    )

    assert len(composed[0]) == 2


async def test_process_empty_page_still_makes_background_tile(tmp_path):
    # An empty upstream gets a provisional placeholder while waiting for covers.
    module = _FakeModule([])
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:albums"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    entry = svc._entries[cat_id]
    assert entry["file"] and (svc._dir / entry["file"]).is_file()
    assert entry["provisional"] is True


async def test_process_browse_error_still_makes_background_tile(tmp_path):
    class _Boom:
        async def browse(self, *args, **kwargs):
            raise RuntimeError("jamendo down")

    svc = _service(tmp_path, resolver=lambda eid: _Boom())
    cat_id = "kalinka:jamendo:catalog:popular-tracks"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    entry = svc._entries[cat_id]
    assert entry["file"] and (svc._dir / entry["file"]).is_file()
    assert entry["provisional"] is True


async def test_provisional_tile_upgrades_when_covers_arrive(tmp_path):
    # First pass: no covers -> provisional background tile.
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule([]))
    cat_id = "kalinka:jamendo:catalog:popular-tracks"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    bg_file = svc._entries[cat_id]["file"]
    assert svc._entries[cat_id]["provisional"] is True

    # Second pass (fresh service on the same dir = a restart) with covers now
    # available -> upgrade, and the old background file is kept so a client still
    # showing it doesn't 404.
    covers = {}
    blob = render.encode_jpeg(_solid_cover((30, 90, 160)))
    for local in ("a1", "a2"):
        path = tmp_path / f"{local}.jpg"
        path.write_bytes(blob)
        covers[f"album/{local}_large.jpg"] = str(path)
    svc2 = _service(
        tmp_path,
        resolver=lambda eid: _FakeModule(
            [_album_child("a1"), _album_child("a2")], covers
        ),
    )
    await svc2._process(cat_id, render.ArtStyle.CARD, hint=None)
    entry = svc2._entries[cat_id]
    assert entry["file"] != bg_file
    assert entry["provisional"] is False
    assert (svc2._dir / bg_file).is_file()


async def test_a_remade_card_keeps_the_files_it_replaced_for_one_more_render(
    tmp_path,
):
    """A listing served just before a render still names the files it
    replaces; deleting them at once leaves that client on 404s."""
    cover = tmp_path / "a1.jpg"
    covers = {"album/a1_large.jpg": str(cover)}
    svc = _service(
        tmp_path, resolver=lambda eid: _FakeModule([_album_child("a1")], covers)
    )
    cat_id = "kalinka:qobuz:catalog:recent"
    generations = []
    for color in ((200, 40, 40), (40, 200, 40), (40, 40, 200)):
        cover.write_bytes(render.encode_jpeg(_solid_cover(color)))
        await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
        entry = svc._entries[cat_id]
        generations.append({entry["file"], entry["thumbnail"]})

    first, second, third = generations
    assert all((svc._dir / name).is_file() for name in second | third)
    assert not any((svc._dir / name).exists() for name in first)
    assert set(svc._entries[cat_id]["previous"]) == second
    # A restart's sweep keeps them too.
    reloaded = _service(tmp_path)
    assert all((reloaded._dir / name).is_file() for name in second | third)


async def test_transient_failure_keeps_existing_full_tile(tmp_path):
    covers = {}
    blob = render.encode_jpeg(_solid_cover((200, 50, 50)))
    for local in ("a1", "a2"):
        path = tmp_path / f"{local}.jpg"
        path.write_bytes(blob)
        covers[f"album/{local}_large.jpg"] = str(path)
    svc = _service(
        tmp_path,
        resolver=lambda eid: _FakeModule(
            [_album_child("a1"), _album_child("a2")], covers
        ),
    )
    cat_id = "kalinka:jamendo:catalog:popular-tracks"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    full_file = svc._entries[cat_id]["file"]
    assert svc._entries[cat_id]["provisional"] is False

    # Upstream goes flaky (empty) -> keep the good tile, don't downgrade it.
    svc2 = _service(tmp_path, resolver=lambda eid: _FakeModule([]))
    await svc2._process(cat_id, render.ArtStyle.CARD, hint=None)
    assert svc2._entries[cat_id]["file"] == full_file
    assert (svc2._dir / full_file).is_file()


async def test_index_reload_drops_missing_files(tmp_path):
    svc = _service(tmp_path)
    svc._dir.mkdir(parents=True, exist_ok=True)
    svc._entries["kalinka:localfiles:catalog:albums"] = {
        "file": "deadbeefdeadbeef-01020304.jpg",
        "fingerprint": "x",
        "next_check_at": 1e18,
    }
    svc._save_index()
    # File was never written → a fresh load drops the dangling entry.
    reloaded = _service(tmp_path)
    assert "kalinka:localfiles:catalog:albums" not in reloaded._entries


def test_index_reload_keeps_thumbnails_and_forgets_lost_ones(tmp_path):
    svc = _service(tmp_path)
    svc._dir.mkdir(parents=True, exist_ok=True)
    for cat_id, stem in (("kept", "a" * 16), ("lost", "b" * 16)):
        (svc._dir / f"{stem}-01020304.jpg").write_bytes(b"card")
        svc._entries[cat_id] = {
            "file": f"{stem}-01020304.jpg",
            "thumbnail": f"{stem}-01020304-thumb.jpg",
            "fingerprint": "x",
            "next_check_at": 1e18,
        }
    (svc._dir / svc._entries["kept"]["thumbnail"]).write_bytes(b"thumb")
    svc._save_index()
    reloaded = _service(tmp_path)
    assert (reloaded._dir / reloaded._entries["kept"]["thumbnail"]).is_file()
    # The card stays, standing in for the thumbnail it lost.
    assert reloaded._entries["lost"]["file"]
    assert "thumbnail" not in reloaded._entries["lost"]


# --------------------------------------------------------------------------
# When to look again
# --------------------------------------------------------------------------


def _distinct_covers(tmp_path, *locals_):
    """Covers that differ in content — identical bytes are deduplicated, so a
    mosaic built from one repeated picture would collapse to a single tile."""
    covers = {}
    for index, local in enumerate(locals_):
        covers.update(
            _covers_on_disk(tmp_path, local, color=(40 * (index + 1), 70, 110))
        )
    return covers


def _due_in(svc, cat_id):
    return svc._entries[cat_id]["next_check_at"] - time.time()


async def test_a_card_short_of_covers_is_looked_at_again_within_the_hour(tmp_path):
    """The regression this exists for: a card built while the library was
    still acquiring artwork gets a thinner mosaic than it asked for. That is
    a real result rather than a failure, so the tile stands — but holding it
    for a day strands the card on art that was only ever momentary."""
    module = _FakeModule(
        [_album_child("a1"), _album_child("a2"), _album_child("a3")],
        _distinct_covers(tmp_path, "a1"),
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:library"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert svc._entries[cat_id]["provisional"] is False
    assert _due_in(svc, cat_id) <= PARTIAL_RETRY_SECONDS


async def test_a_card_with_every_cover_it_wanted_keeps_the_long_refresh(tmp_path):
    """The common case must not start polling hourly for nothing."""
    module = _FakeModule(
        [_album_child(n) for n in ("a1", "a2", "a3")],
        _distinct_covers(tmp_path, "a1", "a2", "a3"),
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:albums"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert _due_in(svc, cat_id) > PARTIAL_RETRY_SECONDS
    assert _due_in(svc, cat_id) <= REFRESH_SECONDS


async def test_a_catalog_smaller_than_the_tile_keeps_the_long_refresh(tmp_path):
    """It gave every cover it has, so it is complete at two. Measuring it
    against the tile's appetite instead would re-compose it hourly for the
    life of the install and never add a thing."""
    module = _FakeModule(
        [_album_child("a1"), _album_child("a2")],
        _distinct_covers(tmp_path, "a1", "a2"),
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:albums"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert svc._entries[cat_id]["provisional"] is False
    assert _due_in(svc, cat_id) > PARTIAL_RETRY_SECONDS


async def test_a_card_with_no_covers_at_all_still_retries_fastest(tmp_path):
    """A thin mosaic is not a failure; no mosaic still is."""
    module = _FakeModule([_album_child("a1")], {})
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:albums"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert svc._entries[cat_id]["provisional"] is True
    assert _due_in(svc, cat_id) <= FAIL_RETRY_SECONDS


async def test_a_library_whose_albums_carry_no_art_yet_retries_soon(tmp_path):
    """The regression this exists for: a library rebuilt minutes ago lists its
    albums before the indexer has attached any artwork, which reads exactly
    like a catalog that has none."""
    module = _FakeModule([_album_child(n, with_image=False) for n in ("a1", "a2")], {})
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:library"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert svc._entries[cat_id]["provisional"] is True
    assert _due_in(svc, cat_id) <= FAIL_RETRY_SECONDS


async def test_a_rebuild_does_not_downgrade_the_tile_it_already_had(tmp_path):
    """The same window seen from the card's side: the art it had is better
    than the background the rebuild can offer, so it stands until the covers
    come back."""
    module = _FakeModule(
        [_album_child(n) for n in ("a1", "a2", "a3")],
        _distinct_covers(tmp_path, "a1", "a2", "a3"),
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:library"
    await svc._process(cat_id, render.ArtStyle.CARD, hint=None)
    full_file = svc._entries[cat_id]["file"]
    assert svc._entries[cat_id]["provisional"] is False

    rebuilding = _FakeModule(
        [_album_child(n, with_image=False) for n in ("a1", "a2")], {}
    )
    svc2 = _service(tmp_path, resolver=lambda eid: rebuilding)
    await svc2._process(cat_id, render.ArtStyle.CARD, hint=None)

    assert svc2._entries[cat_id]["file"] == full_file
    assert (svc2._dir / full_file).is_file()
    assert _due_in(svc2, cat_id) <= FAIL_RETRY_SECONDS


async def test_a_catalog_placeholder_retries_for_real_artwork(tmp_path):
    """A catalog can gain child artwork after its placeholder was made."""
    module = _FakeModule([_catalog_item("albums"), _catalog_item("artists")], {})
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:library"

    await svc._process(cat_id, render.ArtStyle.CARD, hint=PreviewContentType.CATALOG)

    assert svc._entries[cat_id]["provisional"] is True
    assert _due_in(svc, cat_id) <= FAIL_RETRY_SECONDS


class TestARestartRevalidates:
    """A card is a picture of what a catalog holds. Nothing tells the service
    when those contents are replaced — a rebuilt library wipes the module's
    own database and artwork, but this cache lives elsewhere and survived,
    leaving a banner composed from a library that no longer exists. A restart
    is the one moment that can be relied on to follow such a change.
    """

    def _index_with(self, tmp_path, due_in):
        svc = _service(tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        (svc._dir / "abc.jpg").write_bytes(render.encode_jpeg(_solid_cover((1, 2, 3))))
        svc._entries["kalinka:localfiles:catalog:library"] = {
            "file": "abc.jpg",
            "fingerprint": "stale",
            "next_check_at": time.time() + due_in,
            "provisional": False,
        }
        svc._save_index()
        return svc

    def test_a_card_not_due_for_hours_is_checked_again_after_a_restart(self, tmp_path):
        self._index_with(tmp_path, due_in=REFRESH_SECONDS)

        restarted = _service(tmp_path)  # reads the index from disk

        entry = restarted._entries["kalinka:localfiles:catalog:library"]
        assert entry["next_check_at"] == 0.0

    def test_the_existing_tile_is_kept_so_no_card_goes_blank(self, tmp_path):
        """Re-checking must not mean discarding: the client keeps showing the
        old picture until a better one is actually rendered."""
        self._index_with(tmp_path, due_in=REFRESH_SECONDS)

        restarted = _service(tmp_path)

        entry = restarted._entries["kalinka:localfiles:catalog:library"]
        assert entry["file"] == "abc.jpg"
        assert (restarted._dir / "abc.jpg").is_file()

    def test_a_due_card_is_enqueued_on_the_first_browse(self, tmp_path):
        self._index_with(tmp_path, due_in=REFRESH_SECONDS)
        restarted = _service(tmp_path)
        item = _catalog_item("library")
        item.id = EntityId(id="library", type=EntityType.CATALOG, source="localfiles")

        restarted.decorate(BrowseItemList(offset=0, limit=10, total=1, items=[item]))

        assert restarted._queue.qsize() == 1
        # and it still carries the old art meanwhile
        assert item.catalog.image is not None


@pytest.mark.parametrize("kind", ["album", "catalog", "track", "playlist", "artist"])
@pytest.mark.parametrize("square", [False, True])
def test_missing_artwork_uses_one_transparent_object_without_a_collage(
    kind, square, monkeypatch
):
    import numpy as np

    artwork = render.placeholder_object(kind)
    assert artwork.mode == "RGBA"
    alpha = np.asarray(artwork.getchannel("A"))
    assert (alpha == 0).mean() > 0.10
    assert (alpha >= 250).mean() > 0.40

    calls = []

    def load_object(selected):
        calls.append(selected)
        return artwork

    def forbidden_collage(*args, **kwargs):
        pytest.fail("Missing artwork must use an isolated cabinet, not framed tiles")

    monkeypatch.setattr(render, "placeholder_object", load_object)
    monkeypatch.setattr(render, "_collage", forbidden_collage)
    if square:
        image = render.render_catalog_thumbnail(
            [], "fallback", side=120, placeholder=kind
        )
        bare = render.render_catalog_thumbnail([], "fallback", side=120)
    else:
        image = render.render_catalog_art(
            [], [], "fallback", width=360, height=120, placeholder=kind
        )
        bare = render.render_catalog_art([], [], "fallback", width=360, height=120)
        text_column = (0, 0, 209, 120)
        assert image.crop(text_column).tobytes() == bare.crop(text_column).tobytes()
    assert calls == [kind]
    assert image.tobytes() != bare.tobytes()
    assert image.getpixel((0, 0)) == bare.getpixel((0, 0))


@pytest.mark.parametrize("count", [1, 2, 3])
def test_any_real_cover_suppresses_all_placeholders(monkeypatch, count):
    def unexpected(kind):
        pytest.fail("Real artwork must not load any placeholder")

    monkeypatch.setattr(render, "placeholder_object", unexpected)
    covers = [_solid_cover((30 * i, 70, 100)) for i in range(count)]
    for kind in PreviewContentType:
        actual = render.render_catalog_art(
            covers, [], "real", width=300, height=100, placeholder=kind.value
        )
        expected = render.render_catalog_art(covers, [], "real", width=300, height=100)
        assert actual.tobytes() == expected.tobytes()
        actual = render.render_catalog_thumbnail(
            covers, "real", side=100, placeholder=kind.value
        )
        expected = render.render_catalog_thumbnail(covers, "real", side=100)
        assert actual.tobytes() == expected.tobytes()


def test_placeholder_kind_changes_cache_identity_only_without_real_art():
    fingerprints = {
        render.content_fingerprint([], [], "id", placeholder=kind.value)
        for kind in PreviewContentType
    }
    assert len(fingerprints) == len(PreviewContentType)
    assert render.content_fingerprint(
        [b"cover"], [], "id", placeholder="album"
    ) == render.content_fingerprint([b"cover"], [], "id", placeholder="track")


def _child_of_kind(kind, local="one", image=None):
    from kalinka_plugin_sdk.datamodel import Artist

    eid = EntityId(id=local, type=EntityType(kind.value), source="localfiles")
    if kind == PreviewContentType.ALBUM:
        return BrowseItem(
            id=eid, name=local, album=Album(id=eid, title=local, image=image)
        )
    if kind == PreviewContentType.ARTIST:
        return BrowseItem(
            id=eid, name=local, artist=Artist(id=eid, name=local, image=image)
        )
    if kind == PreviewContentType.CATALOG:
        return _catalog_item(local, image=image)
    if kind == PreviewContentType.PLAYLIST:
        item = _collection_item(local, track_count=2)
        item.playlist.image = image
        return item
    album = Album(
        id=EntityId(id="parent", type=EntityType.ALBUM, source="localfiles"),
        title="Parent",
    )
    return BrowseItem(
        id=eid,
        name=local,
        track=Track(id=eid, title=local, duration=60, album=album, image=image),
    )


@pytest.mark.parametrize("kind", list(PreviewContentType))
async def test_actual_contents_select_placeholder_even_with_a_wrong_hint(
    tmp_path, kind
):
    module = _FakeModule([_child_of_kind(kind)])
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    hint = (
        PreviewContentType.TRACK
        if kind == PreviewContentType.CATALOG
        else PreviewContentType.CATALOG
    )
    await svc._process(cat_id, render.ArtStyle.CARD, hint)
    entry = svc._entries[cat_id]
    assert entry["content_type"] == kind.value
    assert entry["has_covers"] is False and entry["provisional"] is True
    expected = render.render_catalog_art([], [], cat_id, placeholder=kind.value)
    assert (svc._dir / entry["file"]).read_bytes() == render.encode_jpeg(expected)
    expected = render.render_catalog_thumbnail([], cat_id, placeholder=kind.value)
    assert (svc._dir / entry["thumbnail"]).read_bytes() == render.encode_jpeg(expected)


@pytest.mark.parametrize("kind", list(PreviewContentType))
async def test_browse_hint_reaches_the_worker_when_contents_are_empty(tmp_path, kind):
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule([]))
    item = _catalog_item(
        "empty", preview=Preview(type=PreviewType.TEXT_ONLY, content_type=kind)
    )
    svc.decorate(BrowseItemList(offset=0, limit=1, total=1, items=[item]))
    job = svc._queue.get_nowait()
    await svc._process(*job)
    assert svc._entries[item.id.to_string]["content_type"] == kind.value


@pytest.mark.parametrize("kind", list(PreviewContentType))
async def test_one_available_image_replaces_the_placeholder_object(tmp_path, kind):
    module = _FakeModule([_child_of_kind(kind)])
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD)
    old_file = svc._entries[cat_id]["file"]
    covers = _covers_on_disk(tmp_path, "one")
    module._covers = covers
    module._children = [
        _child_of_kind(kind, image=CoverImage(large="/resource/album/one_large.jpg")),
        _child_of_kind(kind, "two"),
    ]
    await svc._process(cat_id, render.ArtStyle.CARD, PreviewContentType.CATALOG)
    entry = svc._entries[cat_id]
    assert entry["has_covers"] is True and entry["provisional"] is False
    assert entry["file"] != old_file
    with Image.open(covers["album/one_large.jpg"]) as cover:
        expected = render.render_catalog_art([cover], [], cat_id)
    assert (svc._dir / entry["file"]).read_bytes() == render.encode_jpeg(expected)


@pytest.mark.parametrize("real_first", [False, True])
async def test_changing_the_contents_replaces_the_cached_collage(tmp_path, real_first):
    covers = _covers_on_disk(tmp_path, "one") if real_first else {}
    module = _FakeModule([_album_child("one")], covers)
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD)
    old_file = svc._entries[cat_id]["file"]
    module._children = [_child_of_kind(PreviewContentType.PLAYLIST)]
    await svc._process(cat_id, render.ArtStyle.CARD)
    entry = svc._entries[cat_id]
    assert entry["content_type"] == "playlist"
    assert entry["has_covers"] is False
    assert entry["file"] != old_file


async def test_old_catalog_background_is_upgraded_to_a_placeholder(tmp_path):
    module = _FakeModule([_catalog_item("child")])
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    svc._dir.mkdir(parents=True, exist_ok=True)
    (svc._dir / "old.jpg").write_bytes(
        render.encode_jpeg(render.render_catalog_art([], [], cat_id))
    )
    svc._entries[cat_id] = {
        "file": "old.jpg",
        "provisional": False,
        "fingerprint": "old-style",
    }
    await svc._process(cat_id, render.ArtStyle.CARD)
    assert svc._entries[cat_id]["file"] != "old.jpg"
    assert svc._entries[cat_id]["has_covers"] is False


async def test_mixed_contents_use_the_most_common_kind(tmp_path):
    module = _FakeModule(
        [
            _catalog_item("child"),
            _child_of_kind(PreviewContentType.TRACK, "a"),
            _child_of_kind(PreviewContentType.TRACK, "b"),
        ]
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD)
    assert svc._entries[cat_id]["content_type"] == "track"


async def test_a_failed_browse_keeps_the_previous_placeholder_kind(tmp_path):
    module = _FakeModule([_album_child("one", with_image=False)])
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD)
    old_file = svc._entries[cat_id]["file"]
    module._children = []
    await svc._process(cat_id, render.ArtStyle.CARD)
    assert svc._entries[cat_id]["content_type"] == "album"
    assert svc._entries[cat_id]["file"] == old_file


@pytest.mark.parametrize("failure", ["empty", "error"])
@pytest.mark.parametrize("with_cover", [False, True])
async def test_unavailable_catalog_keeps_observed_kind_over_preview_hint(
    tmp_path, failure, with_cover
):
    image = CoverImage(large="/resource/album/one_large.jpg") if with_cover else None
    module = _FakeModule(
        [_child_of_kind(PreviewContentType.TRACK, image=image)],
        _covers_on_disk(tmp_path, "one") if with_cover else {},
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD, PreviewContentType.ALBUM)
    previous = dict(svc._entries[cat_id])
    assert previous["content_type"] == "track"
    assert previous["has_covers"] is with_cover
    files = {
        name: (svc._dir / name).read_bytes()
        for name in (previous["file"], previous["thumbnail"])
    }

    class _Unavailable:
        async def browse(self, *args, **kwargs):
            if failure == "error":
                raise RuntimeError("Source temporarily unavailable")
            return BrowseItemList(offset=0, limit=10, total=0, items=[])

    reloaded = _service(tmp_path, resolver=lambda eid: _Unavailable())
    before = time.time()
    await reloaded._process(cat_id, render.ArtStyle.CARD, PreviewContentType.ALBUM)
    entry = reloaded._entries[cat_id]
    for field in (
        "file",
        "thumbnail",
        "fingerprint",
        "content_type",
        "has_covers",
        "provisional",
    ):
        assert entry[field] == previous[field]
    assert before + FAIL_RETRY_SECONDS <= entry["next_check_at"]
    assert entry["next_check_at"] <= time.time() + FAIL_RETRY_SECONDS
    assert {name: (reloaded._dir / name).read_bytes() for name in files} == files
    assert _service(tmp_path)._entries[cat_id]["content_type"] == "track"


async def test_unchanged_art_still_records_a_change_in_content_type(tmp_path):
    image = CoverImage(large="/resource/album/one_large.jpg")
    module = _FakeModule(
        [_child_of_kind(PreviewContentType.ALBUM, image=image)],
        _covers_on_disk(tmp_path, "one"),
    )
    svc = _service(tmp_path, resolver=lambda eid: module)
    cat_id = "kalinka:localfiles:catalog:content"
    await svc._process(cat_id, render.ArtStyle.CARD)
    old_file = svc._entries[cat_id]["file"]
    module._children = [_child_of_kind(PreviewContentType.TRACK, image=image)]
    await svc._process(cat_id, render.ArtStyle.CARD)
    assert svc._entries[cat_id]["file"] == old_file
    assert svc._entries[cat_id]["content_type"] == "track"


def test_background_text_normalizes_cosmetic_changes_and_ignores_source_id():
    assert render.background_text_key(
        ["  Cafe\u0301   Albums ", "ALL\nMUSIC"], "source:a"
    ) == render.background_text_key(["café albums", "all music"], "source:b")
    assert render.background_text_key([], "source:a") != render.background_text_key(
        [], "source:b"
    )
    assert render.background_text_key(
        ["Albums", "Jazz"], "id"
    ) == render.background_text_key(["Albums", "Rock"], "id")
    assert render.background_text_key(["Albums"], "id") != render.background_text_key(
        ["Tracks"], "id"
    )


def test_card_text_selects_several_distinct_backgrounds():
    names = [
        "Albums",
        "Artists",
        "Tracks",
        "Playlists",
        "Recently added",
        "Jazz",
        "Classical",
        "Rock",
        "Electronic",
        "Blues",
        "Discover",
        "Genres",
    ]
    images = [
        render.render_catalog_art(
            [], [name], "same-id", width=300, height=100
        ).tobytes()
        for name in names
    ]
    assert len(set(images)) >= 4
    assert (
        render.render_catalog_art([], ["Jazz"], "same-id").tobytes()
        == render.render_catalog_art([], ["Jazz"], "same-id").tobytes()
    )


def test_title_hash_uses_the_full_palette_and_highlight_variations():
    colors = {
        render._background_colors(
            render.background_text_key([f"Music collection {index}"], "same-id")
        )
        for index in range(500)
    }
    assert {accent for accent, _ in colors} == {
        accent for accent, _ in render.BACKGROUND_PALETTES
    }
    assert len(colors) >= 44


def test_titles_with_the_same_shape_and_colors_still_get_distinct_geometry():
    groups = {}
    transforms = set()
    for index in range(500):
        key = render.background_text_key([f"Music collection {index}"], "id")
        transform = render.background_transform(key)
        transforms.add(transform)
        group = (int(key[:16], 16) % 6, render._background_colors(key))
        groups.setdefault(group, []).append(key)
    assert len(transforms) == 500
    collision = next(keys for keys in groups.values() if len(keys) >= 3)
    images = [render._background(360, 120, key).tobytes() for key in collision]
    assert len(set(images)) == len(collision)
    assert render._background(360, 120, collision[0]).tobytes() == images[0]


@pytest.mark.parametrize("variant", range(len(render.BACKGROUND_DESIGNS)))
def test_transform_extremes_keep_shapes_visible_and_caption_clear(variant):
    import numpy as np

    for tail in ("00" * 8, "ff" * 8, "00ff00ff00ffffff", "ff00ff00ff000000"):
        key = f"{variant:016x}{3:016x}{1:016x}{tail}"
        transform = render.background_transform(key)
        source = render._backdrop(variant, transform)
        assert np.ptp(np.asarray(source)[:, 1400:]) > 40
        card = np.asarray(render._background(360, 120, key))
        assert np.ptp(card[:, :209].astype(np.int16), axis=(0, 1)).max() <= 1
        thumb = np.asarray(render._background(120, 120, key, text_column=False))
        assert np.ptp(thumb[..., :3].astype(np.int16), axis=(0, 1)).max() > 10


def test_tonal_mapping_smoothly_joins_each_palette():
    import numpy as np
    from PIL import ImageColor

    source = Image.fromarray(np.arange(256, dtype=np.uint8)[None, :])
    for family in range(len(render.BACKGROUND_PALETTES)):
        for light in range(3):
            key = f"{0:016x}{family:016x}{light:016x}{0:016x}"
            accent, highlight = render._background_colors(key)
            image = render._tint_background(source, key)
            assert image.getpixel((160, 0)) == ImageColor.getrgb(accent)
            assert image.getpixel((255, 0)) == ImageColor.getrgb(highlight)
            pixels = np.asarray(image, dtype=np.int16)
            assert np.abs(np.diff(pixels, axis=1)).max() <= 2


@pytest.mark.parametrize("variation", ["color", "geometry"])
@pytest.mark.parametrize("foreground", ["cover", "cabinet"])
def test_background_changes_leave_foreground_art_unchanged(
    monkeypatch, foreground, variation
):
    artwork = _solid_cover((30, 140, 220))
    monkeypatch.setattr(
        render, "placeholder_object", lambda kind: artwork.convert("RGBA")
    )

    def compose():
        return render.render_catalog_art(
            [artwork] if foreground == "cover" else [],
            ["Albums"],
            "same-id",
            width=360,
            height=120,
            placeholder="catalog",
        )

    first = compose()
    if variation == "color":
        monkeypatch.setattr(
            render, "_background_colors", lambda key: ("#E16051", "#A87935")
        )
    else:
        monkeypatch.setattr(
            render,
            "background_transform",
            lambda key: render.BackgroundTransform(30, -0.07, 0.25, 0.82, 1.2),
        )
    second = compose()
    center = (250, 50, 270, 70) if foreground == "cover" else (288, 57, 302, 72)
    assert first.crop(center).tobytes() == second.crop(center).tobytes()
    assert first.tobytes() != second.tobytes()


def test_card_and_thumbnail_select_the_same_background_by_text(monkeypatch):
    variants = []
    colors = []
    original = render._backdrop
    original_tint = render._tint_background

    def record(variant, transform):
        variants.append((variant, transform))
        return original(variant, transform)

    monkeypatch.setattr(render, "_backdrop", record)

    def tint(source, text_key):
        colors.append(render._background_colors(text_key))
        return original_tint(source, text_key)

    monkeypatch.setattr(render, "_tint_background", tint)
    names = ("Jazz", "Albums from your library")
    render.render_catalog_art([], names, "source:a", width=300, height=100)
    render.render_catalog_thumbnail([], "source:b", names=names, side=100)
    render.render_catalog_art(
        [_solid_cover("red")], names, "source:c", width=300, height=100
    )
    assert len(set(variants)) == 1
    assert len(set(colors)) == 1


def test_background_assignment_survives_python_hash_randomization():
    import os
    import subprocess
    import sys

    script = 'from kalinka_server.catalog_art_render import background_text_key, _background_colors; key = background_text_key(["Jazz", "Albums"], "id"); print(key, _background_colors(key))'
    keys = [
        subprocess.check_output(
            [sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": seed}
        )
        for seed in ("1", "99")
    ]
    assert keys[0] == keys[1]


@pytest.mark.parametrize("variant", range(len(render.BACKGROUND_DESIGNS)))
def test_every_palette_keeps_titles_and_secondary_text_readable(variant):
    import numpy as np

    source = render._backdrop(variant)
    assert source.mode == "L" and source.size == (2400, 800)

    def luminance(rgb):
        values = np.asarray(rgb, dtype=np.float32) / 255
        linear = np.where(
            values <= 0.04045,
            values / 12.92,
            ((values + 0.055) / 1.055) ** 2.4,
        )
        return linear @ np.array([0.2126, 0.7152, 0.0722])

    for family in range(len(render.BACKGROUND_PALETTES)):
        text_key = f"{variant:016x}{family:016x}{2:016x}{0:016x}"
        image = render._background(360, 120, text_key).convert("RGB")
        column = np.asarray(image.crop((0, 0, 209, 120)))
        brightest = float(luminance(column).max())
        assert (luminance([250, 245, 240]) + 0.05) / (brightest + 0.05) >= 7
        assert (luminance([187, 187, 187]) + 0.05) / (brightest + 0.05) >= 4.5
        assert np.ptp(column.astype(np.int16), axis=(0, 1)).max() <= 1


async def _drain_art_jobs(svc):
    import asyncio

    worker = asyncio.create_task(svc.run())
    try:
        await svc._queue.join()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_card_text_reaches_both_renderers_and_renames_refresh_the_cache(tmp_path):
    svc = _service(tmp_path, resolver=lambda eid: _FakeModule([]))
    item = _catalog_item("albums")
    item.catalog.description = "All your music"
    cat_id = item.id.to_string

    async def decorate(title, description):
        item = _catalog_item("albums")
        item.catalog.title = title
        item.catalog.description = description
        svc.decorate(BrowseItemList(offset=0, limit=1, total=1, items=[item]))
        await _drain_art_jobs(svc)
        return svc._entries[cat_id]

    first = await decorate("Albums", "All your music")
    names = ("Albums", "All your music")
    expected = render.render_catalog_art([], names, cat_id, placeholder="catalog")
    assert (svc._dir / first["file"]).read_bytes() == render.encode_jpeg(expected)
    expected = render.render_catalog_thumbnail(
        [], cat_id, names=names, placeholder="catalog"
    )
    assert (svc._dir / first["thumbnail"]).read_bytes() == render.encode_jpeg(expected)
    same = await decorate("  ALBUMS ", "All  your music")
    assert same["file"] == first["file"]
    renamed = await decorate("Jazz albums", "All your music")
    assert renamed["file"] != first["file"]
    described = await decorate("Jazz albums", "From your library")
    assert described["file"] == renamed["file"]
    assert svc._queue.empty()
    assert described["text_key"] == render.background_text_key(
        ["Jazz albums", "From your library"], cat_id
    )


async def test_text_refresh_respects_retry_delay_when_the_source_is_unavailable(
    tmp_path,
):
    svc = _service(tmp_path)
    item = _catalog_item("albums")
    svc.decorate(BrowseItemList(offset=0, limit=1, total=1, items=[item]))
    await _drain_art_jobs(svc)
    svc.decorate(
        BrowseItemList(offset=0, limit=1, total=1, items=[_catalog_item("albums")])
    )
    assert svc._queue.empty()
    renamed = _catalog_item("albums")
    renamed.catalog.title = "Jazz albums"
    svc.decorate(BrowseItemList(offset=0, limit=1, total=1, items=[renamed]))
    assert svc._queue.qsize() == 1
