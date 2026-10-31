import logging
from pathlib import Path
import httpx
from typing import Dict, Optional

from ..config_model import LocalFilesConfig
from ..utils.artwork_store import save_artwork_images
from ..utils.name_utils import (
    name_similarity,
    repair_mojibake,
    space_dotted_abbreviations,
    truncated_name_similarity,
    unescape_web_entities,
)
from .enricher_plugin import (
    EnricherPlugin,
    TransientEnrichmentError,
    best_search_name,
    has_real_cover,
    inferred_claims,
    raise_if_service_unavailable,
    write_album_cover,
)


logger = logging.getLogger(__name__.split(".")[-1])

# Outbound requests in flight at once. The enricher processes several
# entities concurrently; this keeps a burst from reaching the service
# all at once without needing a limiter at each call site.
_MAX_CONCURRENT_REQUESTS = 4

# Waiting for one of those connections is queueing, not a service problem, so
# it gets a long budget of its own: the plugins report a timeout as "service
# unreachable", which ends the whole enrichment pass.
_TIMEOUT = httpx.Timeout(5.0, pool=120.0)


# Fuzzy matching threshold for album and artist matching
FUZZY_MATCH_THRESHOLD = 0.8

#: Deezer marks "no photograph" with the MD5 of the empty string. Only the
#: empty-segment form redirects; the spelled-out one answers 200 with the
#: grey silhouette, so the status code cannot be what decides.
_NO_PICTURE_HASHES = ("d41d8cd98f00b204e9800998ecf8427e", "/artist//")

# Deezer's structured search honours only *quoted* values — an unquoted
# multi-word artist or album returns nothing at all. The free-text fallback
# has to name the artist too: searching the title alone returns other
# people's records, and no amount of scoring recovers from that.
_ALBUM_QUERIES = (
    'artist:"{artist}" album:"{title}"',
    "{artist} {title}",
)


def _is_placeholder_picture(url: str) -> bool:
    """Whether a Deezer image URL is the "no photograph" stand-in."""
    return any(marker in url for marker in _NO_PICTURE_HASHES)


class DeezerPlugin(EnricherPlugin):
    """
    Deezer enrichment plugin for artist images

    DISCLAIMER: This plugin is strictly for personal use. All images fetched from
    Deezer are subject to Deezer's terms of use and copyright restrictions.
    This plugin should not be used in any commercial application or publicly
    distributed software without proper licensing from Deezer.
    """

    # 2: the structured search finally sends quoted values (unquoted found
    # nothing for any multi-word name), the fallback query names the artist,
    # and candidates rank on the weaker of title/artist rather than title
    # alone — so rows this plugin silently could not match re-open.
    ENRICHER_VERSION = 2

    def __init__(self, config: LocalFilesConfig, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.artwork_path = Path(config.artwork_path).expanduser().resolve()

        # Set a proper User-Agent
        user_agent = config.enricher.plugins.user_agent
        # Common headers
        self.headers = {"User-Agent": user_agent}

        # Initialize httpx async client for async requests. Serial enrichment
        # used to be Deezer's implicit rate limit; with entities enriched
        # concurrently the connection pool is what keeps us civil — requests
        # past the cap queue instead of leaving together.
        self.async_client = httpx.AsyncClient(
            headers=self.headers,
            limits=httpx.Limits(max_connections=_MAX_CONCURRENT_REQUESTS),
            timeout=_TIMEOUT,
        )

    def can_enrich_artist(self) -> bool:
        return True

    def can_enrich_album(self) -> bool:
        return True

    def can_enrich_track(self) -> bool:
        return False

    def _find_best_album_match(
        self, albums: list, target_title: str, target_artist: str
    ) -> Optional[tuple]:
        """The candidate that matches on both title and artist, or None.

        Ranking on the title alone lets a covers act with an identical title
        outrank the real release, which the artist test then rejects — so the
        weaker of the two scores both ranks the candidates and decides
        whether the best one is good enough.

        Returns ``(album, title_score, artist_score)``.
        """
        best = None
        for candidate in albums:
            if not candidate.get("title"):
                continue
            candidate_artist = (candidate.get("artist") or {}).get("name", "")
            title_score = name_similarity(candidate["title"], target_title)
            artist_score = truncated_name_similarity(candidate_artist, target_artist)
            combined = min(title_score, artist_score)

            logger.debug(
                f"Deezer candidate '{candidate['title']}' by "
                f"'{candidate_artist or 'Unknown'}' - title {title_score:.2f}, "
                f"artist {artist_score:.2f}"
            )
            if best is None or combined > best[0]:
                best = (combined, candidate, title_score, artist_score)

        if best is None or best[0] < FUZZY_MATCH_THRESHOLD:
            return None
        return best[1], best[2], best[3]

    async def enrich_artist(self, artist: Dict) -> Optional[Dict]:
        """
        Enrich artist with image from Deezer API

        This will only run if the artist doesn't already have an image from
        previous enrichment plugins (like Wikidata)
        """
        # Skip if artist already has an image_url
        if artist.get("image_url"):
            logger.debug(
                f"Artist {artist['name']} already has an image, skipping Deezer lookup"
            )
            return None

        try:
            logger.debug(f"Searching for artist image on Deezer: {artist['name']}")

            # Same tag repair the MusicBrainz search does: "В.Цой" and
            # "&amp;" find nothing as-is.
            search_name = space_dotted_abbreviations(
                unescape_web_entities(
                    repair_mojibake(
                        await best_search_name(
                            self.db_manager, "artist", artist["id"], "name",
                            artist["name"],
                        ),
                        self.config.legacy_tag_encoding,
                    )
                )
            )
            search_url = "https://api.deezer.com/search/artist"
            response = await self.async_client.get(
                search_url, params={"q": search_name, "limit": 10}
            )
            raise_if_service_unavailable(response.status_code, "Deezer")

            if response.status_code != 200:
                logger.error(
                    f"Failed to search Deezer for {artist['name']}: {response.status_code}"
                )
                return None

            data = response.json()
            if "data" not in data or not data["data"]:
                logger.debug(f"No Deezer results found for artist: {artist['name']}")
                return None

            scored = [
                (truncated_name_similarity(candidate["name"], search_name), candidate)
                for candidate in data["data"]
                if candidate.get("name")
            ]
            for name_score, candidate in scored:
                logger.debug(
                    f"Artist '{candidate['name']}' - Name score: {name_score:.2f}"
                )

            best_score = max((score for score, _ in scored), default=0.0)
            if best_score <= FUZZY_MATCH_THRESHOLD:
                logger.debug(
                    f"No artist with score > {FUZZY_MATCH_THRESHOLD} found for: {artist['name']}"
                )
                return None

            # Two acts can share a name outright, and nothing here can tell
            # which one a library holds — so decline rather than guess, and
            # leave the artist to a source that works from its identity.
            tied = [candidate for score, candidate in scored if score == best_score]
            if len(tied) > 1:
                logger.info(
                    f"Deezer has {len(tied)} artists named "
                    f"'{tied[0]['name']}'; declining to guess for {artist['name']}"
                )
                return None

            deezer_artist = tied[0]
            logger.debug(
                f"Best artist match for '{artist['name']}': '{deezer_artist['name']}' (score: {best_score:.2f})"
            )

            # Check if the artist has an image
            picture = deezer_artist.get("picture_xl")
            if not picture or _is_placeholder_picture(picture):
                logger.debug(
                    f"No image available for artist on Deezer: {deezer_artist['name']}"
                )
                return None

            # A redirect still means the placeholder, for any form of it the
            # URL check above does not know.
            image_response = await self.async_client.get(picture)
            raise_if_service_unavailable(image_response.status_code, "Deezer")
            if image_response.is_redirect:
                logger.debug(
                    f"Deezer has only a placeholder image for artist {artist['name']}"
                )
                return None
            if image_response.status_code != 200:
                logger.error(
                    f"Failed to download image for artist {artist['name']}: {image_response.status_code}"
                )
                return None

            # Save the image in different sizes
            image_data = image_response.content
            if self._save_images(image_data, artist["id"], "artist"):
                # Update artist data
                updates = {
                    "image_url": artist["id"],
                }

                logger.debug(f"Added image from Deezer for artist: {artist['name']}")
                return {"updates": updates}
            else:
                return None

        except TransientEnrichmentError:
            raise
        except httpx.TransportError as e:
            raise TransientEnrichmentError(f"Deezer is unreachable: {e}") from e
        except Exception as e:
            logger.error(
                f"Error enriching artist {artist['name']} with Deezer image: {str(e)}"
            )
            return None

    def _save_images(self, image_data: bytes, entity_id: str, entity_type: str) -> bool:
        return save_artwork_images(
            self.artwork_path, image_data, entity_id, entity_type
        )

    async def enrich_album(self, album: Dict) -> Optional[Dict]:
        """Fill an album's cover from Deezer, or leave it alone.

        The structured query is tried first because it is the precise one;
        the free-text query then catches records whose title differs in
        wording from the local tag. Both are scored the same way.
        """
        if has_real_cover(album):
            logger.debug(
                f"Album {album['title']} already has cover art, skipping Deezer lookup"
            )
            return None

        try:
            # Get artist name from various possible sources
            artist_name = await self._get_artist_name(album)
            if not artist_name:
                logger.error(f"Could not find artist name for album: {album['title']}")
                return None

            logger.info(
                f"Searching for album cover on Deezer: {album['title']} by {artist_name}"
            )

            for template in _ALBUM_QUERIES:
                query = template.format(artist=artist_name, title=album["title"])
                match = self._find_best_album_match(
                    await self._search_albums(query), album["title"], artist_name
                )
                if not match:
                    continue
                deezer_album, title_score, artist_score = match
                logger.info(
                    f"Deezer matched '{album['title']}' by '{artist_name}' to "
                    f"'{deezer_album['title']}' by "
                    f"'{deezer_album['artist']['name']}' "
                    f"(title {title_score:.2f}, artist {artist_score:.2f})"
                )
                # A match with no cover is no use here; the next query may
                # find the same record on a release that has one.
                processed = await self._process_album_match(
                    deezer_album, album, artist_name
                )
                if processed:
                    return processed

            logger.info(
                f"No good matches found for album: {album['title']} by {artist_name}"
            )
            return None

        except TransientEnrichmentError:
            raise
        except httpx.TransportError as e:
            raise TransientEnrichmentError(f"Deezer is unreachable: {e}") from e
        except Exception as e:
            logger.error(
                f"Error enriching album {album['title']} with Deezer cover: {str(e)}"
            )
            return None

    async def _get_artist_name(self, album: Dict) -> Optional[str]:
        """The album artist's name, as an external catalogue would spell it."""
        name = album.get("artist_name")
        artist_id = album.get("artist_id")
        if not name and artist_id:
            artist = await self.db_manager.get_artist_by_id(artist_id)
            name = (artist or {}).get("name")
        if not name:
            return None
        if not artist_id:
            return name
        return await best_search_name(
            self.db_manager, "artist", artist_id, "name", name
        )

    async def _search_albums(self, query: str) -> list:
        """One album search against Deezer; ``[]`` when it has nothing to say."""
        response = await self.async_client.get(
            "https://api.deezer.com/search/album",
            params={"q": query, "limit": 10},
        )
        raise_if_service_unavailable(response.status_code, "Deezer")
        if response.status_code != 200:
            logger.error(f"Deezer album search failed: {response.status_code}")
            return []
        return response.json().get("data") or []

    async def _process_album_match(
        self, deezer_album: Dict, album: Dict, artist_name: str
    ) -> Optional[Dict]:
        """Process a matched Deezer album and download artwork"""
        # Check if the album has a cover
        if "cover_xl" not in deezer_album or not deezer_album["cover_xl"]:
            logger.debug(f"No cover available for album on Deezer: {album['title']}")
            return None

        # Download the cover
        image_response = await self.async_client.get(deezer_album["cover_xl"])
        raise_if_service_unavailable(image_response.status_code, "Deezer")
        if image_response.status_code != 200:
            logger.error(
                f"Failed to download cover for album {album['title']}: {image_response.status_code}"
            )
            return None

        updates = {}

        # Save the image in different sizes. The cover is Deezer's only direct
        # write — genre is a resolvable field, so it's emitted as a claim and
        # left to resolution (a local tag or MB outranks it).
        image_data = image_response.content
        if await write_album_cover(
            self.db_manager,
            album["id"],
            lambda current: not has_real_cover(current),
            lambda: self._save_images(image_data, album["id"], "album"),
        ):
            # Cleared alongside the URL: this cover replaces a generated one,
            # and a row still flagged generated is wiped by the art sweep.
            updates["image_url"] = album["id"]
            updates["image_generated"] = 0
            logger.info(
                f"Added cover image from Deezer for album: {album['title']} by {artist_name}"
            )

        # Add genre if available from Deezer
        genre = None
        if "genre_id" in deezer_album and deezer_album.get("genre_id"):
            # Get detailed genre info
            try:
                genre_response = await self.async_client.get(
                    f"https://api.deezer.com/genre/{deezer_album['genre_id']}"
                )
                if genre_response.status_code == 200:
                    genre = genre_response.json().get("name")
            except Exception as genre_error:
                logger.warning(
                    f"Error fetching genre for album {album['title']}: {str(genre_error)}"
                )

        claims = inferred_claims(f"deezer:{deezer_album.get('id', '')}", {"genre": genre})
        return {"updates": updates, "claims": claims}

    async def enrich_track(self, track: Dict) -> Optional[Dict]:
        """Placeholder for track enrichment - not implemented"""
        return None
