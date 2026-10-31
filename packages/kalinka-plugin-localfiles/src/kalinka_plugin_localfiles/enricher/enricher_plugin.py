import abc
import asyncio
from typing import Callable, Dict, List, Optional

from ..resolution.resolver import LOCALLY_DERIVED, TIER_RANK, source_base
from ..utils.artwork_store import artwork_lock


class TransientEnrichmentError(Exception):
    """The service never gave a verdict about the entity — DNS failure,
    refused connection, timeout, or overload/rate-limit responses. Nothing
    was learned about the entity, so its row must stay pending for the next
    cycle rather than be recorded. A response that speaks about the entity
    (found, 404, …) is a verdict and is recorded as usual.
    """


class EntityEnrichmentError(Exception):
    """One entity could not be processed though the service is answering — a
    failed download, an unparseable reply, a bug on this row. The row stays
    pending, because returning nothing is indistinguishable from "no answer"
    and would let a later source fill what this one owed. The service keeps
    its turn on every other row.
    """


def raise_musicbrainz_unreachable(error) -> None:
    """Re-raise a ``musicbrainzngs.NetworkError`` as transient. Verdicts
    (400/404/411) arrive as ``ResponseError``; a NetworkError wrapping an
    HTTP response can only be 5xx that survived the library's own retries —
    MusicBrainz's rate-limit/overload signal, never a statement about the
    entity."""
    raise TransientEnrichmentError(
        f"MusicBrainz is unreachable: {error}"
    ) from error


def raise_if_service_unavailable(status_code: int, service: str) -> None:
    """Turn a throttle or server-side failure into a transient error.

    429 and 5xx describe the service's own state, not the entity: recording
    them as a verdict marks the row terminal for an outage it knew nothing
    about. Every other status — 200, 404, a malformed request — is the
    service answering about the entity and stays the caller's to interpret.
    """
    if status_code == 429 or status_code >= 500:
        raise TransientEnrichmentError(
            f"{service} is unavailable: HTTP {status_code}"
        )


async def best_search_name(
    db_manager, entity_type: str, entity_id: str, field: str, fallback: str
) -> str:
    """The name most likely to find this entity in another catalogue.

    What is displayed is not always what to search with. Resolution keeps a
    name the library observed in a tag over a merely-inferred external claim,
    so a name a legacy fixed-width field cut short — "Иванушки Int" — stays
    cut short on the row, and a provider asked for that finds nothing or
    scores it away. A source that already identified this entity recorded the
    whole name as a claim, and that is the one to ask with.

    @return The highest-tier external claim's value, or ``fallback`` when
        nothing but the library's own reading of the files exists.
    """
    claims = await db_manager.get_claims(entity_type, entity_id, field)
    external = [
        c
        for c in claims
        if c.get("value") and source_base(c.get("source") or "") not in LOCALLY_DERIVED
    ]
    if not external:
        return fallback
    return max(external, key=lambda c: TIER_RANK.get(c.get("tier"), 0))["value"]


async def write_album_cover(
    db_manager,
    album_id: str,
    wanted: Callable[[Dict], bool],
    write: Callable[[], bool],
    generated: bool = False,
) -> bool:
    """Store an album's cover with ``write`` and point the album at it, if
    the album as it stands now is still ``wanted``; say whether it did.

    Files and row change together under the album's artwork lock, which the
    indexer's cover pass also holds: a source decides on a stale snapshot.
    """
    async with artwork_lock(album_id):
        current = await db_manager.get_album_by_id(album_id)
        if current is None or not wanted(current):
            return False
        if not await asyncio.to_thread(write):
            return False
        await db_manager.set_album_image(album_id, generated)
        return True


def has_real_cover(album: Dict) -> bool:
    """Whether an album already carries artwork a real source supplied.

    A generated placeholder fills ``image_url`` like any other cover, so
    testing that column alone reports every album as illustrated and stops
    the fetching sources from ever being asked again.
    """
    return bool(album.get("image_url")) and not album.get("image_generated")


def _claims(source: str, fields: Dict[str, object], tier: str) -> List[Dict]:
    return [
        {"field": field, "value": value, "source": source, "tier": tier}
        for field, value in fields.items()
        if value
    ]


def inferred_claims(source: str, fields: Dict[str, object]) -> List[Dict]:
    """Build the enricher's claim dicts for each non-empty field, attributed to
    ``source`` at the ``inferred`` tier — the shape every plugin emits and the
    enricher resolves. Centralised so the plugin→enricher claim contract lives
    in one place rather than being hand-built per plugin.
    """
    return _claims(source, fields, "inferred")


def verified_claims(source: str, fields: Dict[str, object]) -> List[Dict]:
    """The same, at the tier that may replace a display value outright.

    Reserved for a direct identifier — a fingerprint that names the recording
    itself, not a text search that ranked some candidates. A fuzzy match must
    stay ``inferred`` however high it scored: a score is how a source earns its
    verdict, never a claim to authority over another source.
    """
    return _claims(source, fields, "verified")


class EnricherPlugin(abc.ABC):
    """Base class for enricher plugins"""

    # Bump in a subclass whenever its enrichment logic changes in a way
    # that could turn a previous FAILED into a success (a new matcher
    # tier, a bug fix, a looser heuristic). It feeds the enrichment
    # fingerprint (see ``MetadataEnricher.compute_fingerprint``); a bump
    # re-opens previously-FAILED rows on the next restart. Scoped per
    # plugin, so bumping a plugin the user has *disabled* changes
    # nothing — it never enters the fingerprint.
    ENRICHER_VERSION: int = 1

    #: True for a plugin that *derives* from the entity's resolved identity
    #: rather than fetching something keyed on an identifier — generated art
    #: is drawn from the album's title, artist and genre, so it must not run
    #: before resolution has settled them. Such a plugin runs in a second pass
    #: after resolution, and any claim it emitted would be too late to resolve.
    runs_after_resolution: bool = False

    def config_signature(self) -> Dict:
        """Config fields that affect match *outcomes*, folded into the
        enrichment fingerprint so a change re-opens previously-FAILED
        rows on restart.

        Return only fields that can change whether an entity enriches
        successfully (thresholds, an API key becoming available) — not
        cosmetic ones like debug logging. Default: nothing
        outcome-affecting.
        """
        return {}

    @abc.abstractmethod
    def can_enrich_artist(self) -> bool:
        """Whether this plugin can enrich artist metadata"""
        pass

    @abc.abstractmethod
    def can_enrich_album(self) -> bool:
        """Whether this plugin can enrich album metadata"""
        pass

    @abc.abstractmethod
    def can_enrich_track(self) -> bool:
        """Whether this plugin can enrich track metadata"""
        pass

    @abc.abstractmethod
    async def enrich_artist(self, artist: Dict) -> Optional[Dict]:
        """Enrich artist metadata"""
        pass

    @abc.abstractmethod
    async def enrich_album(self, album: Dict) -> Optional[Dict]:
        """Enrich album metadata"""
        pass

    @abc.abstractmethod
    async def enrich_track(self, track: Dict) -> Optional[Dict]:
        """Enrich track metadata"""
        pass
