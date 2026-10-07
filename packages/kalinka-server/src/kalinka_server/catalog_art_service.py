"""Server-generated art for browse items that have none of their own.

``decorate()`` runs inline on every ``/browse`` response: for each imageless
catalog item it fills in the URL of ready art or enqueues generation, never
blocking. What it generates depends on what the item is — a card background
for a category, a square mosaic cover for a list of tracks. A single worker
drains the queue — browsing the item's first page, rendering via
:mod:`catalog_art_render`, and writing the JPEG under
``<prefix>/var/cache/kalinka/catalog_art/``. File names embed a content
fingerprint, so a card's URL changes only when its content does; the worker
re-checks every ``REFRESH_SECONDS`` but re-renders only on a fingerprint move.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from PIL import Image

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    CoverImage,
    EntityId,
    PreviewContentType,
    PreviewType,
)
from kalinka_plugin_sdk.inputmodule import InputModule

from .browse_source import BrowseSource
from .catalog_art_render import (
    ArtStyle,
    content_fingerprint,
    encode_jpeg,
    render_catalog_art,
    render_playlist_cover,
)

logger = logging.getLogger(__name__.split(".")[-1])

ART_URL_PREFIX = "/catalog/art"

#: Re-check a card's inputs this often; a no-op unless the content changed.
REFRESH_SECONDS = 24 * 3600
#: After a failed attempt (module down, remote cache warming) retry sooner.
FAIL_RETRY_SECONDS = 10 * 60
#: A card short of the covers it wanted is a real result rather than a failure,
#: so its tile stands — but a day is too long to hold one composed while the
#: library was still acquiring artwork.
PARTIAL_RETRY_SECONDS = 60 * 60
FETCH_LIMIT = 8  # one upstream page per card
MAX_COVERS = 3
#: A mosaic wants four distinct albums, so it looks further down the list.
COVER_FETCH_LIMIT = 24
COVER_TILES = 4
MAX_COVER_BYTES = 8 * 1024 * 1024

_FILE_RE = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{8}\.jpg$")


def _item_image_path(item: BrowseItem) -> Optional[str]:
    """Best cover path/URL of a browse item, mirroring the client getter."""
    image: Optional[CoverImage] = None
    if item.album is not None:
        image = item.album.image
    elif item.artist is not None:
        image = item.artist.image
    elif item.playlist is not None:
        image = item.playlist.image
    elif item.catalog is not None:
        image = item.catalog.image
    elif item.track is not None and item.track.album is not None:
        image = item.track.album.image
    if image is None:
        return None
    return image.large or image.small or image.thumbnail


def _item_album_id(item: BrowseItem) -> Optional[str]:
    """The album a browse item's cover belongs to, where it names one. One
    album's tracks may each carry their own cover URL — Jamendo stamps the track
    id into the query — so a URL alone cannot tell two artworks apart."""
    album = item.album
    if album is None and item.track is not None:
        album = item.track.album
    return album.id.to_string if album is not None else None


def _has_image(item: BrowseItem) -> bool:
    catalog = item.catalog
    if catalog is None or catalog.image is None:
        return False
    return bool(catalog.image.large or catalog.image.small or catalog.image.thumbnail)


def _style_of(item: BrowseItem) -> ArtStyle:
    """A browse item that is itself a list of tracks wants a cover; a category
    of them wants the wide card background."""
    return ArtStyle.COVER if item.playlist is not None else ArtStyle.CARD


def _is_folder(item: BrowseItem) -> bool:
    preview = item.catalog.preview_config if item.catalog else None
    return preview is not None and preview.type == PreviewType.FOLDER


def _source_owns_art(item: BrowseItem) -> bool:
    """A folder listed as a row has art its source composed, or none for the
    client to draw: a card made from its first page would be neither. A
    folder catalog that stands on a home surface — it has a role — is a card
    like any other."""
    return _is_folder(item) and item.catalog.role is None


def _textual_hint(item: BrowseItem) -> bool:
    preview = item.catalog.preview_config if item.catalog else None
    if preview is None:
        return False
    return (
        preview.type == PreviewType.TEXT_ONLY
        or preview.content_type == PreviewContentType.CATALOG
    )


def _write_atomic(target: Path, data: bytes) -> None:
    """Write *data* to *target* via a temp file in the same dir, renamed into
    place. The temp is removed if the rename never happens, so an interrupted
    write leaves no ``.tmp`` litter behind."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class CatalogArtService:
    """Generates, caches and serves composed catalog-card backgrounds."""

    def __init__(
        self,
        cache_dir: str,
        browse_resolver: Callable[[EntityId], Optional[BrowseSource]],
        resource_resolver: Callable[[EntityId], Optional[InputModule]],
    ) -> None:
        self._dir = Path(cache_dir)
        self._browse = browse_resolver
        self._resource = resource_resolver
        self._queue: asyncio.Queue[tuple[str, ArtStyle, bool]] = asyncio.Queue(
            maxsize=128
        )
        self._pending: set[str] = set()
        self._http: Optional[httpx.AsyncClient] = None
        # catalog id -> {"file": str|None, "fingerprint": str, "next_check_at": float}
        self._entries: dict[str, dict] = {}
        self._load_index()

    # ------------------------------------------------------------------
    # Index persistence
    # ------------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self._dir / "index.json"

    def _load_index(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text())
            self._entries = dict(raw.get("entries", {}))
            # Nothing reports a catalog being replaced wholesale — a library
            # rebuilt, a module reconfigured — and a restart is what follows
            # such a change, so every card is due again. The existing tile
            # stands until the check is done, and an unmoved one renders
            # nothing.
            for entry in self._entries.values():
                entry["next_check_at"] = 0.0
        except FileNotFoundError:
            self._entries = {}
        except (OSError, ValueError) as exc:
            logger.warning("Catalog art index unreadable, starting clean: %s", exc)
            self._entries = {}
        # Drop entries whose file vanished and files no entry references.
        referenced = set()
        for cat_id, entry in list(self._entries.items()):
            name = entry.get("file")
            if name:
                if (self._dir / name).is_file():
                    referenced.add(name)
                else:
                    del self._entries[cat_id]
        try:
            # *.png/*.tmp sweep transparent-era and interrupted-write leftovers.
            for pattern in ("*.jpg", "*.png", "*.tmp"):
                for path in self._dir.glob(pattern):
                    if path.name not in referenced:
                        path.unlink(missing_ok=True)
        except OSError:
            pass

    def _save_index(self) -> None:
        try:
            payload = json.dumps({"entries": self._entries}).encode()
            _write_atomic(self._index_path, payload)
        except OSError as exc:
            logger.warning("Could not persist catalog art index: %s", exc)

    # ------------------------------------------------------------------
    # Inline decoration (fast path, no I/O)
    # ------------------------------------------------------------------

    def decorate(self, result: BrowseItemList) -> None:
        """Fill in generated art URLs on catalog items that have no image of
        their own; enqueue (re-)generation where needed. Never blocks."""
        for item in result.items:
            if (
                item.catalog is None
                or not item.can_browse
                or _has_image(item)
                or _source_owns_art(item)
            ):
                continue
            # A list with nothing in it has nothing to compose; its cover is
            # the client's to draw.
            if item.playlist is not None and item.playlist.track_count == 0:
                continue
            cat_id = item.id.to_string if isinstance(item.id, EntityId) else str(item.id)
            entry = self._entries.get(cat_id)
            if entry and entry.get("file"):
                url = f"{ART_URL_PREFIX}/{entry['file']}"
                image = CoverImage(small=url, thumbnail=url, large=url)
                item.catalog.image = image
                # The art is the entity's, not one payload's: read as a
                # playlist, the item shows it too.
                if item.playlist is not None:
                    item.playlist.image = image
            if entry is None or time.time() >= entry.get("next_check_at", 0.0):
                self._schedule(cat_id, _style_of(item), _textual_hint(item))

    def _schedule(self, cat_id: str, style: ArtStyle, textual: bool) -> None:
        if cat_id in self._pending:
            return
        try:
            self._queue.put_nowait((cat_id, style, textual))
            self._pending.add(cat_id)
        except asyncio.QueueFull:
            logger.debug("Catalog art queue full; skipping %s", cat_id)

    # ------------------------------------------------------------------
    # Serving
    # ------------------------------------------------------------------

    def art_file(self, file_name: str) -> Optional[Path]:
        """Validated path of a generated file, or None (bad name / missing)."""
        if not _FILE_RE.match(file_name):
            return None
        path = self._dir / file_name
        return path if path.is_file() else None

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain the generation queue for the server's lifetime."""
        while True:
            cat_id, style, textual = await self._queue.get()
            try:
                await self._process(cat_id, style, textual)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(cat_id)
                logger.warning("Catalog art generation failed for %s: %r", cat_id, exc)
            finally:
                self._pending.discard(cat_id)
                self._queue.task_done()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _record_failure(self, cat_id: str) -> None:
        entry = self._entries.setdefault(cat_id, {})
        entry["next_check_at"] = time.time() + FAIL_RETRY_SECONDS
        self._save_index()

    async def _process(self, cat_id: str, style: ArtStyle, textual: bool) -> None:
        entity_id = EntityId.from_string(cat_id)
        module = self._browse(entity_id)
        if module is None:
            self._record_failure(cat_id)
            return

        cover_style = style is ArtStyle.COVER
        items: list[BrowseItem] = []
        try:
            children = await module.browse(
                entity_id,
                offset=0,
                limit=COVER_FETCH_LIMIT if cover_style else FETCH_LIMIT,
            )
            items = children.items or []
        except Exception as exc:
            # The source is flaky (Jamendo especially); fall through with no
            # items -> a background-only tile now, covers added on a later retry.
            logger.debug("Catalog art browse failed for %s: %r", cat_id, exc)

        # A folder carries covers of its own, so a page of them is no index.
        catalog_children = sum(
            1 for item in items if item.catalog is not None and not _is_folder(item)
        )
        textual = not cover_style and (textual or catalog_children > len(items) / 2)

        wanted = COVER_TILES if cover_style else MAX_COVERS
        covers: list[Image.Image] = []
        cover_bytes: list[bytes] = []
        # Distinct covers this catalog offered, which bounds what the card can
        # ever show however often it is composed.
        offered = 0
        if items and not textual:
            seen_paths: set[str] = set()
            seen_albums: set[str] = set()
            seen_art: set[bytes] = set()
            for item in items:
                path = _item_image_path(item)
                if not path:
                    continue
                album = _item_album_id(item)
                if path in seen_paths or (album is not None and album in seen_albums):
                    continue
                seen_paths.add(path)
                if album is not None:
                    seen_albums.add(album)
                blob = await self._fetch_cover(path, item)
                if blob is None:
                    continue
                # Two albums may still wear one picture, and the same picture
                # twice in a mosaic reads as a fault rather than as a cover.
                digest = hashlib.sha1(blob).digest()
                if digest in seen_art:
                    continue
                seen_art.add(digest)
                try:
                    cover = Image.open(io.BytesIO(blob))
                    cover.load()
                except Exception:
                    continue
                covers.append(cover)
                cover_bytes.append(blob)
                if len(covers) >= wanted:
                    break
            offered = len(seen_paths)

        # A cover is its albums and nothing else, so with none to compose there
        # is no art to ship — the client draws its own stand-in. Try again soon
        # in case the source was merely down.
        if cover_style and not covers:
            self._record_failure(cat_id)
            return

        # A card that wants a cascade and has none is held, not finished: a
        # library rebuilt minutes ago offers no cover either, and filing that
        # as complete strands the card on a bare background for a day.
        provisional = (not textual) and not covers
        if provisional:
            next_check = FAIL_RETRY_SECONDS
        elif not textual and len(covers) < min(wanted, offered):
            # Short of what the catalog *had*, so a fetch is what fell short
            # and may not next time. A catalog with fewer albums than the tile
            # holds is already complete, and re-composing it hourly for the
            # life of the install would never add a thing.
            next_check = PARTIAL_RETRY_SECONDS
        else:
            next_check = REFRESH_SECONDS

        entry = self._entries.get(cat_id)
        have_file = bool(
            entry and entry.get("file") and (self._dir / entry["file"]).is_file()
        )

        # A transient failure must not downgrade a good tile: keep the existing
        # (non-provisional) art and just retry soon.
        if provisional and have_file and not entry.get("provisional"):
            entry["next_check_at"] = time.time() + next_check
            self._save_index()
            return

        fingerprint = content_fingerprint(cover_bytes, [], cat_id, style)
        if entry and entry.get("fingerprint") == fingerprint and have_file:
            entry["next_check_at"] = time.time() + next_check
            self._save_index()
            return

        if cover_style:
            image = await asyncio.to_thread(render_playlist_cover, covers)
        else:
            image = await asyncio.to_thread(render_catalog_art, covers, [], cat_id)
        data = await asyncio.to_thread(encode_jpeg, image)

        file_name = (
            f"{hashlib.sha1(cat_id.encode()).hexdigest()[:16]}-{fingerprint[:8]}.jpg"
        )
        _write_atomic(self._dir / file_name, data)

        # Keep the previous file when upgrading a provisional tile, so a client
        # still showing it doesn't 404 before it re-fetches the richer one.
        old_file = entry.get("file") if entry else None
        if old_file and old_file != file_name and not (entry and entry.get("provisional")):
            (self._dir / old_file).unlink(missing_ok=True)

        self._entries[cat_id] = {
            "file": file_name,
            "fingerprint": fingerprint,
            "next_check_at": time.time() + next_check,
            "provisional": provisional,
        }
        self._save_index()
        logger.info(
            "Generated %s art for %s (%s, %d covers%s)",
            style.value,
            cat_id,
            file_name,
            len(covers),
            ", provisional" if provisional else "",
        )

    async def _fetch_cover(self, path: str, item: BrowseItem) -> Optional[bytes]:
        """Cover bytes for a child item: local resources straight from disk
        via the owning module, remote URLs over HTTP."""
        if path.startswith("http://") or path.startswith("https://"):
            if self._http is None:
                self._http = httpx.AsyncClient(
                    follow_redirects=True, timeout=httpx.Timeout(10.0)
                )
            try:
                response = await self._http.get(path)
                response.raise_for_status()
                if len(response.content) > MAX_COVER_BYTES:
                    return None
                return response.content
            except httpx.HTTPError as exc:
                logger.debug("Cover fetch failed for %s: %r", path, exc)
                return None

        # A module may hang a query off its link; the resource it names is the
        # path before it, and a query read as part of the name resolves nothing.
        resource = path.split("?", 1)[0]
        resource = resource.removeprefix("/resource/").removeprefix("resource/")
        item_id = (
            item.id
            if isinstance(item.id, EntityId)
            else EntityId.from_string(str(item.id))
        )
        module = self._resource(item_id)
        if module is None:
            return None
        try:
            file_path = await module.get_resource_path(resource)
            if not file_path:
                return None
            return await asyncio.to_thread(Path(file_path).read_bytes)
        except Exception as exc:
            logger.debug("Local cover read failed for %s: %r", path, exc)
            return None
