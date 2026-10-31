import logging
from pathlib import Path
from typing import Dict, Optional

from ..config_model import LocalFilesConfig
from ..utils.artwork_store import ArtworkSave, store_decoded_image
from .enricher_plugin import EnricherPlugin, write_album_cover

logger = logging.getLogger(__name__.split(".")[-1])

# Rendered once at this size, then downscaled — matches the `_large`
# artwork size the other art-fetching plugins (Deezer) produce.
_LARGE_SIZE = 600


class ProceduralArtworkPlugin(EnricherPlugin):
    """Last-resort album artwork: deterministic generated abstract art.

    Runs after every metadata/artwork source has had its chance. When an
    album still has no ``image_url``, renders procedural artwork derived
    from the album's identity (artist + title + track list) and saves it
    in the same three sizes and location the downloaded covers use, so
    the serving path needs no changes.

    Rows it decorates are flagged ``image_generated=1``; the FAILED-row
    retry sweep clears generated art before re-running the plugins, so a
    real cover found by an improved matcher replaces the generated one
    (and identical art is re-derived otherwise — the generator is
    deterministic).
    """

    ENRICHER_VERSION = 1
    # The art is derived from the album's title, artist and genre, so it is
    # drawn once those have resolved — not from a folder-derived title
    # MusicBrainz is about to correct.
    runs_after_resolution = True

    def __init__(self, config: LocalFilesConfig, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.artwork_path = Path(config.artwork_path).expanduser().resolve()

        # Deferred import: pulls in numpy, which is an on-demand optional
        # package — enricher.py only loads this plugin once numpy is
        # importable, but keep the heavy import out of module import time.
        from ..procedural_artwork import ProceduralArtworkGenerator

        self._generator = ProceduralArtworkGenerator()

    def config_signature(self) -> Dict:
        # A generator_version bump changes what the art looks like, so it
        # must re-open FAILED rows (and, via the image_generated sweep,
        # regenerate previously generated covers).
        return {"generator_version": self._generator.generator_version}

    def can_enrich_artist(self) -> bool:
        return False

    def can_enrich_album(self) -> bool:
        return True

    def can_enrich_track(self) -> bool:
        return False

    async def enrich_artist(self, artist: Dict) -> Optional[Dict]:
        return None

    async def enrich_track(self, track: Dict) -> Optional[Dict]:
        return None

    async def enrich_album(self, album: Dict) -> Optional[Dict]:
        if album.get("image_url"):
            return None

        title = album.get("title")
        if not title:
            return None

        try:
            from ..procedural_artwork import AlbumArtworkInput

            track_titles = await self.db_manager.get_album_track_titles(album["id"])
            album_input = AlbumArtworkInput(
                artist=album.get("artist_name") or "",
                title=title,
                genre=album.get("genre"),
                # albums.mbid is a *release* MBID (not a release-group), so
                # it feeds the edition identity; family identity comes from
                # the artist/title/track-list fallback key.
                release_id=album.get("mbid"),
                track_titles=tuple(track_titles),
            )
            image = await self._generator.generate(album_input, size=_LARGE_SIZE)
            saved = await write_album_cover(
                self.db_manager,
                album["id"],
                lambda current: not current.get("image_url"),
                lambda: self._save_images(image, album["id"]),
                generated=True,
            )
        except Exception as e:
            logger.error(
                f"Error generating artwork for album {album.get('title', album['id'])}: {e}"
            )
            return None
        if not saved:
            return None

        logger.debug(f"Generated procedural artwork for album: {title}")
        return {"updates": {"image_url": album["id"], "image_generated": 1}}

    def _save_images(self, image, album_id: str) -> bool:
        """Save the rendered cover in the three standard sizes."""
        if image.mode != "RGB":
            image = image.convert("RGB")
        outcome = store_decoded_image(image, self.artwork_path, album_id, "album")
        return outcome is ArtworkSave.SAVED
