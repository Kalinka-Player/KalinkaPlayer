import logging
from pathlib import Path
from typing import Dict, Optional

import httpx

from ..config_model import LocalFilesConfig
from ..utils.artwork_store import save_artwork_images
from .enricher_plugin import (
    EnricherPlugin,
    TransientEnrichmentError,
    has_real_cover,
    raise_if_service_unavailable,
    write_album_cover,
)

logger = logging.getLogger(__name__.split(".")[-1])

# Requests above the cap queue on the pool under their own long budget (see
# deezer_plugin); the read budget is generous because archive.org is slow.
_MAX_CONCURRENT_REQUESTS = 4
_TIMEOUT = httpx.Timeout(15.0, pool=120.0)

# The archive's pre-rendered thumbnail closest above our largest stored size
# (600px): full-resolution scans can run to many megabytes.
_FRONT_URL = "https://coverartarchive.org/release/{mbid}/front-1200"
# Cover art is a property of the artwork, not of one pressing, so the group
# answers for releases nobody has photographed. MusicBrainz frequently picks
# an obscure pressing out of dozens that share a sleeve.
_GROUP_FRONT_URL = "https://coverartarchive.org/release-group/{mbid}/front-1200"


class CoverArtArchivePlugin(EnricherPlugin):
    """Album covers from the Cover Art Archive, keyed by the MusicBrainz
    release id an earlier plugin resolved.

    Runs after the fetching sources and before the procedural generator: it
    only fills albums that still have no real cover, and it treats a
    generated placeholder as absence, so a later pass can upgrade it. This
    is the cover source for music Deezer does not carry — free/CC releases
    that MusicBrainz nonetheless catalogues.
    """

    ENRICHER_VERSION = 1

    def __init__(self, config: LocalFilesConfig, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.artwork_path = Path(config.artwork_path).expanduser().resolve()
        self.async_client = httpx.AsyncClient(
            headers={"User-Agent": config.enricher.plugins.user_agent},
            limits=httpx.Limits(max_connections=_MAX_CONCURRENT_REQUESTS),
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    def can_enrich_artist(self) -> bool:
        return False  # the archive stores release art only

    def can_enrich_album(self) -> bool:
        return True

    def can_enrich_track(self) -> bool:
        return False

    async def enrich_artist(self, artist: Dict) -> Optional[Dict]:
        return None

    async def enrich_track(self, track: Dict) -> Optional[Dict]:
        return None

    async def _fetch(self, url: str, mbid: str) -> Optional[httpx.Response]:
        """The archive's answer for one URL, or None when it has no art.

        A 404 is an answer — this entity has no cover — while a throttle or
        server fault is not, and is raised so the row stays pending.
        """
        try:
            response = await self.async_client.get(url)
        except httpx.HTTPError as e:
            raise TransientEnrichmentError(
                f"Cover Art Archive is unreachable: {e}"
            ) from e

        raise_if_service_unavailable(response.status_code, "Cover Art Archive")
        if response.status_code == 404:
            logger.debug(f"No archived cover for {mbid}")
            return None
        if response.status_code != 200:
            logger.warning(
                f"Cover Art Archive returned {response.status_code} for {mbid}"
            )
            return None
        return response

    async def enrich_album(self, album: Dict) -> Optional[Dict]:
        if has_real_cover(album):
            return None
        mbid = album.get("mbid")
        if not mbid:
            return None

        response = await self._fetch(_FRONT_URL.format(mbid=mbid), mbid)
        if response is None:
            # This pressing has no cover of its own; ask what the sleeve is.
            rg_id = await self.db_manager.get_release_group_for_release(mbid)
            if not rg_id:
                return None
            response = await self._fetch(_GROUP_FRONT_URL.format(mbid=rg_id), rg_id)
            if response is None:
                return None

        if not await write_album_cover(
            self.db_manager,
            album["id"],
            lambda current: not has_real_cover(current),
            lambda: save_artwork_images(
                self.artwork_path, response.content, album["id"], "album"
            ),
        ):
            return None
        logger.info(
            f"Added cover from the Cover Art Archive for album: "
            f"{album.get('title', album['id'])}"
        )
        return {"updates": {"image_url": album["id"], "image_generated": 0}}
