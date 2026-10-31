#!/usr/bin/env python3
import multiprocessing
import asyncio
import enum
from collections import deque
import importlib.util
import json
import logging
import time
from typing import Dict, NamedTuple, Optional, Tuple

from ..config_model import LocalFilesConfig
from ..resolution.resolver import (
    OBSERVED,
    TAG_CONSENSUS,
    Claim,
    resolve_display_name,
    resolve_field,
)
from ..resolution.tag_consensus import album_tag_consensus
from ..clustering.classify import strip_artist_prefix
from ..worker_utils import nudge

from .enricher_plugin import (
    EntityEnrichmentError,
    TransientEnrichmentError,
    has_real_cover,
)
from .service_backoff import ServiceBackoff
from .service_timings import ServiceTimings
from .musicbrainz_plugin import MusicBrainzPlugin
from .acoustid_plugin import AcoustIdPlugin
from .wikidata_plugin import WikidataPlugin
from .coverartarchive_plugin import CoverArtArchivePlugin
from .deezer_plugin import DeezerPlugin
from .enricher_db import AsyncEnricherDb

logger = logging.getLogger("enricher")

# Set MusicBrainzNGS log level to warning to reduce verbosity
musicbrainz_logger = logging.getLogger("musicbrainzngs")
musicbrainz_logger.setLevel(logging.WARNING)
aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

# REQUIRED = the genuinely-local fields that gate ENRICHED vs FAILED (Phase
# 2e). A cluster whose local identity resolves is done — "ENRICHED
# (local-only)", not FAILED — even with no external match or cover. This
# deletes the old failure mode "correct MB match without a cover ⇒ FAILED".
ARTIST_REQUIRED_FIELDS = ["name"]
ALBUM_REQUIRED_FIELDS = ["title", "artist_id"]
TRACK_REQUIRED_FIELDS = ["title", "artist_id", "album_id", "duration", "track_number"]

# DESIRED = the full target set (the previous required lists). Reaching it
# means external matching succeeded and covers are filled, so the enricher can
# stop early; NOT reaching it is fine — mbid/image_url/year/genre are desirable,
# not required, and their absence never fails a row.
ARTIST_DESIRED_FIELDS = ["name", "mbid", "image_url"]
ALBUM_DESIRED_FIELDS = ["title", "artist_id", "mbid", "image_url", "year", "genre"]
TRACK_DESIRED_FIELDS = [
    "title", "artist_id", "album_id", "mbid", "duration", "track_number",
]

# The plugin-*fetched* subset of ALBUM_DESIRED. genre/year are written by
# resolution after the plugin loop (from claims, not a plugin direct-write), so
# they must not gate whether we keep calling fetch plugins — only these do. The
# full DESIRED set still gates the top-level "already done, skip the pass" check.
ALBUM_FETCH_FIELDS = ["title", "artist_id", "mbid", "image_url"]


def _changed(snapshot: Dict, updated: Dict) -> Dict:
    """What enrichment changed: the rest of the snapshot may be stale."""
    return {
        field: value
        for field, value in updated.items()
        if field not in snapshot or snapshot[field] != value
    }


# Committed with the cover's files by the source that stored them.
_COVER_COLUMNS = ("image_url", "image_generated")


def _album_changes(snapshot: Dict, updated: Dict) -> Dict:
    changes = _changed(snapshot, updated)
    for field in _COVER_COLUMNS:
        changes.pop(field, None)
    return changes


def _album_has(album: Dict, field: str) -> bool:
    """One desired field's presence, counting a generated cover as absent."""
    if field == "image_url":
        return has_real_cover(album)
    return bool(album.get(field))


# Origin/era fields resolved external-first (Phase 2d slice 3). A local tag
# value is an `observed` claim; a fuzzy MB match is `inferred`, so for
# genre/year/language the local tag wins when present, while country/area/
# original_year (no competing local tag) are filled by MB. See resolver
# `_EXTERNAL_FIRST_FIELDS`.
ARTIST_EXTERNAL_FIELDS = ("country", "area")
ALBUM_EXTERNAL_FIELDS = ("genre", "year", "original_year", "language")

# Global variables to manage enricher state. The enricher runs inside the
# librarian process (Phase 2b); the indexer feeds it "enrich"/"stop" over an
# in-process asyncio queue, and the searcher nudge stays cross-process.
_enricher_task: Optional[asyncio.Task] = None
_enricher_queue: Optional[asyncio.Queue] = None
# Woken after each pass: the searcher re-tags, the embedder picks up the
# clap_text jobs the pass just made schedulable.
_searcher_nudge_queue: Optional[multiprocessing.Queue] = None
_embedder_nudge_queue: Optional[multiprocessing.Queue] = None
_shutdown_event = asyncio.Event()


# How many entities of one kind are enriched concurrently. The phases stay
# ordered (artists -> albums -> tracks) because each feeds the next; within a
# phase the entities are independent, so this is what keeps MusicBrainz's
# 1 req/s slot busy instead of idle between entities, and lets the other
# services and local work overlap that wait. Small on purpose: the gain is
# bounded by MB's fixed rate, while every extra slot costs a thread (MB calls
# run via to_thread) and RAM on a Pi.
ENTITY_CONCURRENCY = 6

#: Passes a row may be held pending before it settles for what did answer.
#: Without a ceiling a lookup that always fails holds its row back for good.
MAX_DEFERRALS = 5


class PhaseResult(NamedTuple):
    """One phase's outcome: how many rows were given a verdict, and which
    rows were left pending because a service never answered. The ids (not a
    count) so a pass that revisits a row does not tally it twice."""

    processed: int
    deferred_ids: frozenset


class EnrichmentStatus(enum.IntEnum):
    """Enum for enrichment status values"""

    NOT_ENRICHED = 0  # Initial state, needs enrichment
    ENRICHED = 1  # Successfully enriched with all required fields
    FAILED = 2  # Failed enrichment, missing required fields after all plugins


class MetadataEnricher:
    """Main enricher class that processes database entries using async"""

    #: Entities of one kind enriched at a time; the config overrides it.
    entity_concurrency: int = ENTITY_CONCURRENCY
    #: Rows the last pass left pending on an unavailable service.
    deferred_rows: int = 0

    def __init__(self, config: LocalFilesConfig, db_manager: AsyncEnricherDb):
        self.config = config
        self.db_manager = db_manager
        self.running = False
        self.lock = asyncio.Lock()
        self.plugins = []
        self.entity_concurrency = max(1, config.enricher.concurrency)
        self.deferred_rows = 0

        logger.info("Initializing MetadataEnricher plugins...")

        # Order is the pipeline: the metadata sources get their turn, then
        # the fingerprint rescues whatever they could not identify, then the
        # artwork sources fill what is left. The chain stops early once a
        # track has every desired field and no filename guess still needs
        # fingerprint rescue.
        if config.enricher.plugins.musicbrainz.enabled:
            logger.info("Loading MusicBrainzPlugin")
            self.plugins.append(MusicBrainzPlugin(config, self.db_manager))

        if config.enricher.plugins.wikidata.enabled:
            logger.info("Loading WikidataPlugin")
            self.plugins.append(WikidataPlugin(config, self.db_manager))

        if config.enricher.plugins.deezer.enabled:
            logger.info("Loading DeezerPlugin")
            self.plugins.append(DeezerPlugin(config, self.db_manager))

        # Last of the identity sources: reading the audio is the expensive
        # answer, and only worth paying for when the cheap ones failed or
        # only had a filename to go on.
        if config.enricher.plugins.acoustid.api_key:
            logger.info("Loading AcoustIdPlugin")
            self.plugins.append(AcoustIdPlugin(config, self.db_manager))

        # After the fetching sources, before the generator: it only fills
        # covers nothing else supplied, keyed by the MB release id.
        if config.enricher.plugins.coverartarchive.enabled:
            logger.info("Loading CoverArtArchivePlugin")
            self.plugins.append(CoverArtArchivePlugin(config, self.db_manager))

        # Generated album art is strictly last: it only fills covers no
        # real source could provide, so every fetching plugin above must
        # get its chance first.
        if config.enricher.plugins.procedural_artwork.enabled:
            if importlib.util.find_spec("numpy") is not None:
                logger.info("Loading ProceduralArtworkPlugin")
                from .procedural_artwork_plugin import ProceduralArtworkPlugin

                self.plugins.append(ProceduralArtworkPlugin(config, self.db_manager))
            else:
                logger.warning(
                    "Generated album art is enabled but numpy is not "
                    "installed; skipping. Use 'Restart with install' on the "
                    "modules page to fetch it."
                )

        logger.info(
            f"Loaded {len(self.plugins)} enrichment plugins: {[p.__class__.__name__ for p in self.plugins]}"
        )

    @property
    def _backoff(self) -> ServiceBackoff:
        """Per-service cooldowns, created on first use so the enricher can
        be built without its constructor (tests) and still back off."""
        backoff = self.__dict__.get("_service_backoff")
        if backoff is None:
            backoff = ServiceBackoff()
            self.__dict__["_service_backoff"] = backoff
        return backoff

    @property
    def _deferrals(self) -> Dict[str, int]:
        """Times each row has been held pending, created on first use for the
        same reason as :attr:`_backoff`. Not persisted: a restart means new
        code or a new setup, which deserves a clean try.
        """
        counts = self.__dict__.get("_deferral_counts")
        if counts is None:
            counts = {}
            self.__dict__["_deferral_counts"] = counts
        return counts

    @property
    def _timings(self) -> ServiceTimings:
        """Per-service wall time for the pass in progress, created on first
        use for the same reason as :attr:`_backoff`."""
        timings = self.__dict__.get("_service_timings")
        if timings is None:
            timings = ServiceTimings()
            self.__dict__["_service_timings"] = timings
        return timings

    def compute_fingerprint(self) -> str:
        """Stable signature of the *active* enrichment setup.

        Built from ``self.plugins`` in load order — so it captures which
        plugins are enabled and in what order (first match wins), each
        one's ``ENRICHER_VERSION`` (bumped when its matching code
        changes), and its ``config_signature()`` (match-affecting config
        such as MB thresholds or whether an AcoustID key is set).

        Only enabled plugins contribute, which is the whole point:
        bumping the version of, or reconfiguring, a plugin the user has
        *disabled* leaves the fingerprint untouched and so triggers no
        needless retry sweep. The fingerprint changing is the signal
        that previously-FAILED rows deserve another attempt.
        """
        components = [
            {
                "name": p.__class__.__name__,
                "version": p.ENRICHER_VERSION,
                "config": p.config_signature(),
            }
            for p in self.plugins
        ]
        return json.dumps(components, sort_keys=True)

    async def start(self):
        """Start the enricher process for general enrichment"""
        async with self.lock:
            if self.running:
                logger.warning("Enricher already running, skipping")
                return
            self.running = True

        try:
            # Idle-path logs demoted to DEBUG: the indexer sends "enrich"
            # after every scheduled scan (default every 5 min), so on a
            # quiescent library the user used to see 11 INFO lines of
            # nothing-to-do noise per pass.
            logger.debug("Starting enrichment process")
            await self.run_enrichment()
            logger.debug("Enricher process completed")
        except Exception as e:
            logger.exception(f"Error running enricher: {e}")
        finally:
            async with self.lock:
                self.running = False

    async def run_enrichment(self):
        """Run the enrichment process.

        A transport-level failure (a service unreachable, not answering) is
        never a verdict about the entity, so the row it happened on keeps
        ``NOT_ENRICHED`` and the pass moves on to the next one. The service
        itself is stood down for a growing interval, so an outage costs a
        few probes rather than one failed request per row, and the entities
        other sources can still answer for keep enriching meanwhile.
        """
        totals = {"artists": 0, "albums": 0, "tracks": 0}
        deferred_ids: set = set()
        try:
            while True:
                logger.debug("Processing artists for enrichment")
                artists = await self._process_artists()
                logger.debug("Processing albums for enrichment")
                albums = await self._process_albums()
                logger.debug("Processing tracks for enrichment")
                tracks = await self._process_tracks()

                totals["artists"] += artists.processed
                totals["albums"] += albums.processed
                totals["tracks"] += tracks.processed
                deferred_ids |= (
                    artists.deferred_ids | albums.deferred_ids | tracks.deferred_ids
                )

                total_updates = (
                    artists.processed + albums.processed + tracks.processed
                )

                if total_updates == 0:
                    logger.debug("No more items to process, finishing enrichment")
                    break
        except TransientEnrichmentError as e:
            # Safety net: the plugin chain handles these per plugin, so one
            # reaching here means an unexpected raise path.
            logger.warning(
                "Enrichment paused — %s; pending rows will be retried on the "
                "next cycle",
                e,
            )

        self.deferred_rows = len(deferred_ids)

        # Single INFO summary, only when the pass actually did work. On
        # an idle library this stays silent entirely.
        if any(totals.values()) or deferred_ids:
            logger.info(
                "Enrichment pass complete: %d artists, %d albums, %d tracks "
                "updated, %d row(s) awaiting an unavailable service",
                totals["artists"],
                totals["albums"],
                totals["tracks"],
                len(deferred_ids),
            )

    def _log_service_timings(self, kind: str) -> None:
        """Report where a phase's time went, worst source first, and start
        the next phase's tally.

        Reporting per phase rather than per pass is what makes this usable
        on a slow library: the phases run in sequence, so a pass-level
        summary only arrives once the longest one has finished. Workers
        overlap, so the shares are of summed call time, not of the phase's
        duration.
        """
        timings = self._timings
        total = timings.total_seconds
        summary = timings.summary()
        timings.reset()
        if not total:
            return
        breakdown = ", ".join(
            f"{service} {seconds:.0f}s/{calls} call(s)"
            f" ({seconds / total:.0%}, {seconds / calls:.2f}s avg)"
            for service, calls, seconds in summary
        )
        logger.info("Enrichment time by source (%s): %s", kind, breakdown)

    def next_retry_delay(self) -> Optional[float]:
        """Seconds until deferred rows are worth another attempt, or None
        when nothing is waiting on a stood-down service. This is what lets a
        retry happen on the outage's own timescale instead of waiting for the
        next library scan."""
        if not self.deferred_rows:
            return None
        return self._backoff.next_retry_in()

    async def _run_plugin_chain(
        self, entity_id, updated, claims, can_enrich, enrich, is_complete,
        after_resolution: bool = False,
    ) -> Tuple[bool, bool]:
        """Run the plugin chain for one entity, isolating service outages.

        A plugin whose service is unreachable is skipped — the next plugin
        still gets its turn, because a dead MusicBrainz says nothing about
        whether Deezer can answer. Its service is then stood down for a
        while (see :class:`ServiceBackoff`) so the rest of the pass doesn't
        re-discover the outage row by row.

        Returns ``(had_updates, deferred)``. ``deferred`` means some service
        never answered for this entity, so the caller must not record a
        verdict on it — the row stays pending for a later retry. Whether it
        may be held *again* is the caller's to decide, once per pass: see
        :meth:`_still_worth_deferring`, which spends a row's allowance.

        ``after_resolution`` selects which half of the chain to run: the
        fetching plugins, or the ones that derive from the resolved entity
        (see ``EnricherPlugin.runs_after_resolution``). The two never mix.
        """
        had_updates = False
        deferred = False
        for plugin in self.plugins:
            if plugin.runs_after_resolution != after_resolution:
                continue
            if not can_enrich(plugin):
                continue

            service = plugin.__class__.__name__
            if self._backoff.is_cooling(service):
                deferred = True
                continue

            try:
                with self._timings.measure(service):
                    result = await enrich(plugin, updated)
            except TransientEnrichmentError as e:
                delay = self._backoff.record_failure(service)
                logger.warning(
                    "%s unavailable (%s); standing it down for %.0fs and "
                    "continuing with the other sources",
                    service, e, delay,
                )
                deferred = True
                continue
            except EntityEnrichmentError as e:
                # The service is fine, this row is not: hold the row, and let
                # the service answer for everything else.
                logger.warning(
                    "%s could not answer for %s (%s); leaving it pending",
                    service, entity_id, e,
                )
                deferred = True
                continue

            self._backoff.record_success(service)
            if result:
                claims.extend(result.get("claims") or [])
            if result and "updates" in result:
                updated.update(result["updates"])
                had_updates = True
                if is_complete(updated):
                    logger.debug("%s has every desired field", entity_id)
                    updated["enriched"] = EnrichmentStatus.ENRICHED
                    break
        return had_updates, deferred

    def _still_worth_deferring(self, row_id: Optional[str], deferred: bool) -> bool:
        """Whether a row held pending by a silent source may be held again.

        Waiting is right while a source might still answer, but one that
        never does would hold its row back for good; past the ceiling the row
        settles for the verdict the other sources support.
        """
        if not row_id:
            return deferred
        if not deferred:
            self._deferrals.pop(row_id, None)
            return False
        held = self._deferrals.get(row_id, 0) + 1
        if held <= MAX_DEFERRALS:
            self._deferrals[row_id] = held
            return True
        # Only rows still being waited on are counted, so one re-opened later
        # starts with its full allowance.
        del self._deferrals[row_id]
        logger.warning(
            "%s has been held pending %d times; recording what did answer",
            row_id, held,
        )
        return False

    async def _process_phase(self, kind: str, fetch, enrich) -> "PhaseResult":
        """Drain one entity kind through a pool of concurrent workers.

        ``fetch(limit)`` returns the next pending rows and ``enrich(row)``
        processes one. Workers take the next row the moment they finish one,
        rather than waiting for a whole batch: entity cost varies wildly (an
        album may need one MusicBrainz request or ten), so a batch barrier
        leaves most workers idle behind the slowest row — and with a shared
        1 req/s budget, an idle worker is unused request budget.

        Rows within a phase are independent; the ordering that matters is
        between phases, which ``run_enrichment`` preserves. Rows in flight
        carry no status yet, so they are held in ``in_flight`` to keep a
        refill from handing the same row to a second worker.

        A row whose enrichment was deferred (a service never answered) also
        keeps no status, so it too would come straight back from the next
        fetch: ``deferred`` holds those ids for the rest of the phase, which
        is what lets the phase finish instead of re-serving the same rows
        forever. They stay NOT_ENRICHED in the database and are retried on a
        later pass.
        """
        concurrency = self.entity_concurrency
        buffer: deque = deque()
        in_flight: set[str] = set()
        processed: set[str] = set()
        deferred: set[str] = set()
        fetch_lock = asyncio.Lock()
        failures: list[BaseException] = []

        batch = max(concurrency * 4, concurrency)

        async def take_next() -> Optional[dict]:
            async with fetch_lock:
                if not buffer:
                    # Deferred and in-flight rows are still pending in the
                    # database, so the query keeps returning them; ask for
                    # them *plus* a batch, or the phase would stop at the
                    # first window once everything in it was deferred and
                    # leave the rest of the library untried.
                    rows = await fetch(len(deferred) + len(in_flight) + batch)
                    buffer.extend(
                        r for r in rows
                        if r["id"] not in in_flight and r["id"] not in deferred
                    )
                if not buffer:
                    return None
                row = buffer.popleft()
                in_flight.add(row["id"])
                return row

        async def worker() -> None:
            while not failures:
                row = await take_next()
                if row is None:
                    return
                try:
                    if await enrich(row):
                        deferred.add(row["id"])
                    else:
                        processed.add(row["id"])
                except BaseException as e:  # noqa: BLE001 - re-raised below
                    failures.append(e)
                finally:
                    in_flight.discard(row["id"])

        await asyncio.gather(*(worker() for _ in range(concurrency)))

        if failures:
            for extra in failures[1:]:
                logger.warning(
                    "Additional %s failure in the same pass: %s", kind, extra
                )
            raise failures[0]
        logger.debug(
            "Enriched %d %s, deferred %d", len(processed), kind, len(deferred)
        )
        self._log_service_timings(kind)
        return PhaseResult(len(processed), frozenset(deferred))

    async def _process_artists(self) -> int:
        """Process non-enriched artists"""
        return await self._process_phase(
            "artists",
            self.db_manager.get_non_enriched_artists,
            self._enrich_artist,
        )

    async def _enrich_artist(self, artist) -> bool:
        """Enrich a single artist.

        Returns True when the row was deferred — a source never answered, so
        it keeps no verdict and stays pending for a later retry.
        """
        logger.debug(
            f"Starting enrichment for artist {artist.get('name', artist['id'])}"
        )
        updated_artist = artist.copy()  # Make a copy to carry updates between plugins
        stored = artist.copy()  # as read, to write back only what changed
        had_updates = False

        # Desired = the full target set (id + cover). Reaching it lets us skip
        # the plugins entirely; not reaching it is fine (see the tail).
        is_desired_complete = all(
            updated_artist.get(field) for field in ARTIST_DESIRED_FIELDS
        )
        if is_desired_complete:
            logger.debug(f"Artist {artist['id']} already has all desired fields")
            updated_artist["enriched"] = EnrichmentStatus.ENRICHED
            await self.db_manager.update_artist(
                artist["id"], {"enriched": EnrichmentStatus.ENRICHED}
            )
            return

        logger.debug(
            f"Artist {artist.get('name', artist['id'])} missing fields - starting plugin enrichment"
        )
        emitted_claims: list[dict] = []
        chain_updates, deferred = await self._run_plugin_chain(
            artist.get("name", artist["id"]),
            updated_artist,
            emitted_claims,
            lambda p: p.can_enrich_artist(),
            lambda p, e: p.enrich_artist(e),
            lambda e: all(e.get(f) for f in ARTIST_DESIRED_FIELDS),
        )
        had_updates = had_updates or chain_updates
        deferred = self._still_worth_deferring(artist["id"], deferred)

        # Resolve the display name from claims: a matching external match
        # supplies canonical casing without replacing a different local name.
        if await self._resolve_display_field(
            "artist", artist, updated_artist, emitted_claims, "name"
        ):
            had_updates = True

        # Resolve external-first origin fields (country/area) from claims.
        if await self._resolve_external_fields(
            "artist", artist, updated_artist, emitted_claims, ARTIST_EXTERNAL_FIELDS
        ):
            had_updates = True

        # Status decision (Phase 2e): if external matching didn't fill every
        # desired field, the artist is still ENRICHED when its required local
        # fields resolved — only a missing required field is a FAILURE.
        if updated_artist.get("enriched") != EnrichmentStatus.ENRICHED:
            # A service that never answered leaves no verdict to record: the
            # row keeps NOT_ENRICHED (with whatever other sources did fill in)
            # and is retried once the service is back. Recording local-only
            # here would close the row for good on a passing outage.
            if deferred:
                if had_updates:
                    await self.db_manager.update_artist(
                        artist["id"], _changed(stored, updated_artist)
                    )
                return True
            if all(updated_artist.get(f) for f in ARTIST_REQUIRED_FIELDS):
                updated_artist["enriched"] = EnrichmentStatus.ENRICHED  # local-only
            else:
                missing = [f for f in ARTIST_REQUIRED_FIELDS if not updated_artist.get(f)]
                logger.debug("Artist %s failed enrichment - missing: %s",
                             artist["id"], ", ".join(missing))
                updated_artist["enriched"] = EnrichmentStatus.FAILED
            had_updates = True

        # Only update the database once at the end if we had any updates
        if had_updates:
            await self.db_manager.update_artist(
                artist["id"], _changed(stored, updated_artist)
            )

    async def _local_origin(
        self, entity_type: str, entity_id: str, field: str
    ) -> Tuple[str, str]:
        """How strongly the row's current value is held, as (source, tier).

        A value the indexer read off the path is a ``guessed`` claim from
        ``filename``/``folder_name`` and an external source may correct it;
        one read from a tag is ``observed`` and may not. A row with no
        recorded origin predates provenance and is treated as a tag, which is
        what it was.
        """
        origin = await self.db_manager.get_resolved_origin(
            entity_type, entity_id, field
        )
        if not origin:
            return TAG_CONSENSUS, OBSERVED
        return origin["source"], origin["tier"]

    async def _resolve_display_field(
        self, entity_type: str, entity: dict, updated: dict,
        emitted_claims: list, field: str,
    ) -> bool:
        """Resolve a display-identity field (artist ``name`` / album|track
        ``title``) from claims: an external match re-cases a matching local value
        but never replaces a different one, and never invents one from a fuzzy
        match (§7 — display identity is locally derived). Provenance lands in
        resolved_origin. Returns True if the value changed.

        How strongly the local value is held comes from that same table. A
        value the indexer read off the path is a guess, and correcting a
        filename's typos is precisely what consulting an external source is
        for; a value read from a tag keeps §7's protection. The baseline is
        read from ``updated`` rather than ``entity`` because this pass can
        still refine it first (an album title loses its artist prefix), and
        resolving against the stale row would revert that."""
        local_value = updated.get(field)
        if not local_value:
            return False

        entity_id = entity["id"]
        local_source, local_tier = await self._local_origin(
            entity_type, entity_id, field
        )
        external = []
        for c in emitted_claims:
            if c.get("field") != field:
                continue
            await self.db_manager.record_claim(
                entity_type, entity_id, field, c["value"], c["source"], c["tier"]
            )
            external.append(
                Claim(field, c["value"], c["source"], c["tier"],
                      c.get("evidence_ref"))
            )

        winner = resolve_display_name(
            local_value, external, field=field,
            local_source=local_source, local_tier=local_tier,
        )
        await self.db_manager.record_resolved_origin(
            entity_type, entity_id, field, winner.source, winner.tier,
            winner.evidence_ref,
        )
        if winner.value != updated.get(field):
            updated[field] = winner.value
            logger.debug("%s %s resolved: %r -> %r (%s)", entity_type, field,
                         local_value, winner.value, winner.source)
            return True
        return False

    async def _resolve_external_fields(
        self,
        entity_type: str,
        entity: dict,
        updated: dict,
        emitted_claims: list,
        fields,
        local_overrides: Optional[dict] = None,
    ) -> bool:
        """Resolve external-first origin/era fields from claims (Phase 2d slice
        3). For each field, the local value is an `observed` tag_consensus claim
        and each emitted external value is `inferred`; the resolver picks the
        winner (local tag beats fuzzy MB for genre/year/language; MB fills
        country/area/original_year uncontested). Records the winner in
        resolved_origin and returns True if any value changed.

        ``local_overrides`` (Phase 2g) supplies the local value from actual tag
        evidence (§6.5 consensus) instead of the album row — the genuine
        observed claim — and, being evidence-derived, is persisted as a
        tag_consensus claim for provenance."""
        entity_id = entity["id"]
        overrides = local_overrides or {}
        ext_by_field: dict[str, list] = {}
        for c in emitted_claims:
            f = c.get("field")
            if f in fields:
                ext_by_field.setdefault(f, []).append(c)

        changed = False
        for field in fields:
            externals = ext_by_field.get(field, [])
            local_value = overrides.get(field, entity.get(field))
            claims = []
            if local_value not in (None, ""):
                if field in overrides:
                    # Genuine tag evidence, so it is persisted as a claim.
                    source, tier = TAG_CONSENSUS, OBSERVED
                    await self.db_manager.record_claim(
                        entity_type, entity_id, field, local_value, source, tier,
                    )
                else:
                    # The row's own value, which the indexer may have read off
                    # the path — a year in a folder name is a guess, and MB's
                    # year should correct it.
                    source, tier = await self._local_origin(
                        entity_type, entity_id, field
                    )
                claims.append(Claim(field, local_value, source, tier))
            for c in externals:
                await self.db_manager.record_claim(
                    entity_type, entity_id, field, c["value"], c["source"], c["tier"]
                )
                claims.append(
                    Claim(field, c["value"], c["source"], c["tier"],
                          c.get("evidence_ref"))
                )
            if not claims:
                continue

            winner = resolve_field(claims, current_value=local_value)
            await self.db_manager.record_resolved_origin(
                entity_type, entity_id, field, winner.source, winner.tier,
                winner.evidence_ref,
            )
            if winner.value != updated.get(field):
                updated[field] = winner.value
                changed = True
        return changed

    async def _strip_album_artist_prefix(self, album: dict, updated: dict) -> bool:
        """Drop a leading artist name the folder left on the album title.
        Returns True if the title changed. ``strip_artist_prefix`` only cuts at
        a word boundary and never empties the title, so an eponymous album
        ("Boston - Boston") keeps its name."""
        artist_name = album.get("artist_name")
        title = updated.get("title")
        if not artist_name or not title:
            return False
        cleaned = strip_artist_prefix(title, artist_name)
        if cleaned == title:
            return False
        logger.debug("Album title artist-prefix stripped: %r -> %r", title, cleaned)
        # Correct the entity too, not just the working copy: the stripped form
        # *is* the local observed title, and display-field resolution reads its
        # local value from the entity — leaving it stale would restore the
        # prefix straight after the plugin loop.
        album["title"] = cleaned
        updated["title"] = cleaned
        return True

    # Album origin/era fields with a local tag source (original_year has none).
    _ALBUM_TAG_FIELDS = ("genre", "year", "language")

    async def _album_tag_consensus(self, album_id: str) -> dict:
        """The album's genre/year/language as its tracks' tags agree (§6.5)."""
        track_tags = await self.db_manager.get_album_track_tags(album_id)
        return album_tag_consensus(track_tags, self._ALBUM_TAG_FIELDS)

    async def _process_albums(self) -> int:
        """Process non-enriched albums"""
        return await self._process_phase(
            "albums",
            self.db_manager.get_non_enriched_albums,
            self._enrich_album,
        )

    async def _enrich_album(self, album) -> bool:
        """Enrich a single album.

        Returns True when the row was deferred — a source never answered, so
        it keeps no verdict and stays pending for a later retry.
        """
        updated_album = album.copy()  # Make a copy to carry updates between plugins
        stored = album.copy()  # as read, to write back only what changed
        had_updates = False

        # Desired = full target set (external id, cover, year, genre). Reaching
        # it lets us skip the plugins; not reaching it is fine (see the tail).
        is_desired_complete = all(
            _album_has(updated_album, field) for field in ALBUM_DESIRED_FIELDS
        )
        if is_desired_complete:
            logger.debug(f"Album {album['id']} already has all desired fields")
            updated_album["enriched"] = EnrichmentStatus.ENRICHED
            await self.db_manager.update_album(
                album["id"], {"enriched": EnrichmentStatus.ENRICHED}
            )
            return False

        # An untagged rip's album title is folder-derived and keeps the artist
        # prefix ("The Beatles - Abbey Road") until clustering learns the
        # artist — which for such libraries only happens after the filename
        # fallback runs. Searching a provider with that stale title fails, and
        # by the time the title is corrected the album is already enriched.
        # Strip it here, where the album's artist is known, so matching sees the
        # real title on this pass.
        if await self._strip_album_artist_prefix(album, updated_album):
            had_updates = True

        emitted_claims: list[dict] = []
        # genre/year are resolved after the chain, so they must not gate it —
        # only the plugin-fetched fields do.
        chain_updates, deferred = await self._run_plugin_chain(
            album.get("title", album["id"]),
            updated_album,
            emitted_claims,
            lambda p: p.can_enrich_album(),
            lambda p, e: p.enrich_album(e),
            lambda e: all(_album_has(e, f) for f in ALBUM_FETCH_FIELDS),
        )
        had_updates = had_updates or chain_updates

        # Resolve the display title from claims: a matching external match
        # supplies canonical casing without replacing a different local title.
        if await self._resolve_display_field(
            "album", album, updated_album, emitted_claims, "title"
        ):
            had_updates = True

        # Resolve external-first origin/era fields (genre/year/original_year/
        # language): local tags win over fuzzy MB where present. The local tag
        # value comes from tag-evidence consensus (§6.5), not the album row.
        consensus = await self._album_tag_consensus(album["id"])
        if await self._resolve_external_fields(
            "album", album, updated_album, emitted_claims, ALBUM_EXTERNAL_FIELDS,
            local_overrides=consensus,
        ):
            had_updates = True

        # Spent before the derived plugins run, because what they draw is
        # written once and never redrawn: they get their turn only when
        # nothing is still expected to change the title and genre they draw
        # from — and the pass that stops waiting is such a turn.
        deferred = self._still_worth_deferring(album["id"], deferred)

        # Now that title and genre have settled, the plugins that draw from
        # them get their turn. Nothing here completes the entity, so this pass
        # has no early break.
        if not deferred:
            derived_updates, derived_deferred = await self._run_plugin_chain(
                album.get("title", album["id"]),
                updated_album,
                emitted_claims,
                lambda p: p.can_enrich_album(),
                lambda p, e: p.enrich_album(e),
                lambda e: False,
                after_resolution=True,
            )
            had_updates = had_updates or derived_updates
            deferred = self._still_worth_deferring(album["id"], derived_deferred)

        # Status decision (Phase 2e): an album whose required local fields
        # (title + artist_id) resolved is ENRICHED even without an external
        # match/cover/year/genre — "ENRICHED (local-only)". FAILED only when a
        # required local field is still missing.
        if updated_album.get("enriched") != EnrichmentStatus.ENRICHED:
            # A service that never answered leaves no verdict to record: the
            # row keeps NOT_ENRICHED (with whatever other sources did fill in)
            # and is retried once the service is back. Recording local-only
            # here would close the row for good on a passing outage.
            if deferred:
                if had_updates:
                    await self.db_manager.update_album(
                        album["id"], _album_changes(stored, updated_album)
                    )
                return True
            if all(updated_album.get(f) for f in ALBUM_REQUIRED_FIELDS):
                updated_album["enriched"] = EnrichmentStatus.ENRICHED  # local-only
            else:
                missing = [f for f in ALBUM_REQUIRED_FIELDS if not updated_album.get(f)]
                logger.debug("Album %s failed enrichment - missing: %s",
                             album["id"], ", ".join(missing))
                updated_album["enriched"] = EnrichmentStatus.FAILED
            had_updates = True

        # Only update the database once at the end if we had any updates
        if had_updates:
            await self.db_manager.update_album(
                album["id"], _album_changes(stored, updated_album)
            )

    async def _process_tracks(self) -> int:
        """Process non-enriched tracks"""
        return await self._process_phase(
            "tracks",
            self.db_manager.get_non_enriched_tracks,
            self._enrich_track,
        )

    async def _enrich_track(self, track) -> bool:
        """Enrich a single track.

        Returns True when the row was deferred — a source never answered, so
        it keeps no verdict and stays pending for a later retry.
        """
        updated_track = track.copy()  # Make a copy to carry updates between plugins
        stored = track.copy()  # as read, to write back only what changed
        had_updates = False

        # Having every field filled is not enough when a filename guess was
        # merely echoed by a text search. Let the fingerprint rescue run even
        # if MusicBrainz fills the last missing field earlier in this chain.
        rescue_required = False
        for plugin in self.plugins:
            if isinstance(plugin, AcoustIdPlugin) and plugin.can_enrich_track():
                rescue_required = await plugin.needs_rescue(track)
                break

        # Desired = full target set (incl. mbid). Reaching it lets us skip the
        # plugins; not reaching it is fine (see the tail).
        is_desired_complete = all(
            updated_track.get(field) for field in TRACK_DESIRED_FIELDS
        )
        if is_desired_complete and not rescue_required:
            logger.debug(f"Track {track['id']} already has all desired fields")
            updated_track["enriched"] = EnrichmentStatus.ENRICHED
            await self.db_manager.update_track(
                track["id"], {"enriched": EnrichmentStatus.ENRICHED}
            )
            return

        emitted_claims: list[dict] = []
        chain_updates, deferred = await self._run_plugin_chain(
            track.get("title", track["id"]),
            updated_track,
            emitted_claims,
            lambda p: p.can_enrich_track(),
            lambda p, e: p.enrich_track(e),
            lambda e: not rescue_required
            and all(e.get(f) for f in TRACK_DESIRED_FIELDS),
        )
        had_updates = had_updates or chain_updates
        deferred = self._still_worth_deferring(track["id"], deferred)

        # Resolve the display title from claims: an external match (e.g.
        # AcoustID) re-cases a matching local title but never replaces or invents
        # one (§7). Mostly a no-op that records the title's provenance.
        if await self._resolve_display_field(
            "track", track, updated_track, emitted_claims, "title"
        ):
            had_updates = True

        # Status decision (Phase 2e): a track whose required local fields
        # resolved is ENRICHED even without an mbid — "ENRICHED (local-only)".
        # FAILED only when a required local field is still missing.
        if updated_track.get("enriched") != EnrichmentStatus.ENRICHED:
            # See the artist/album tails: no answer means no verdict, so the
            # row stays pending rather than being closed as local-only.
            if deferred:
                if had_updates:
                    await self.db_manager.update_track(
                        track["id"], _changed(stored, updated_track)
                    )
                return True
            missing = [f for f in TRACK_REQUIRED_FIELDS if not updated_track.get(f)]
            if not missing:
                updated_track["enriched"] = EnrichmentStatus.ENRICHED  # local-only
            else:
                # file_path identifies the track even when title/artist are missing
                where = updated_track.get("file_path") or track["id"]
                logger.debug(
                    "Track %s failed enrichment - missing: %s",
                    where,
                    ", ".join(missing),
                )
                updated_track["enriched"] = EnrichmentStatus.FAILED
            had_updates = True

        # Only update the database once at the end if we had any updates
        if had_updates:
            await self.db_manager.update_track(
                track["id"], _changed(stored, updated_track)
            )
            if "album_id" in updated_track:
                await self.db_manager.update_album_stats(updated_track["album_id"])


async def _enricher_worker(config, db_manager: AsyncEnricherDb):
    """Background worker task for the enricher"""

    if _enricher_queue is None:
        logger.error("Enricher queue is not initialized; stopping worker")
        _shutdown_event.set()
        return

    enricher_instance = MetadataEnricher(config, db_manager)

    # One-time, *gated* retry of previously-FAILED rows. A plain restart
    # no longer re-hammers MusicBrainz/Deezer for rows that will fail
    # identically: FAILED rows are re-opened only when the enrichment
    # fingerprint changed since the last run — i.e. a plugin was
    # enabled/disabled/reordered, a plugin's ENRICHER_VERSION was bumped
    # (new matcher code), or a match-affecting config field changed (an
    # AcoustID key added, an MB threshold lowered). The first run after
    # this ships has no stored fingerprint, so it resets once and then
    # settles.
    try:
        fingerprint = enricher_instance.compute_fingerprint()
        retried = await db_manager.reset_failed_for_fingerprint(fingerprint)
        if retried is None:
            logger.info(
                "Enrichment fingerprint unchanged; leaving FAILED rows as-is"
            )
        else:
            logger.info(
                "Enrichment setup changed; reset %d previously-FAILED rows for "
                "retry: %d artists, %d albums, %d tracks",
                sum(retried.values()),
                retried["artists"],
                retried["albums"],
                retried["tracks"],
            )
    except Exception as e:
        logger.warning(f"Failed-row retry reset skipped: {e}")

    async def run_pass() -> Optional[float]:
        """Run one enrichment pass; returns seconds until deferred rows are
        worth retrying, or None when nothing is waiting on a service."""
        await enricher_instance.start()
        nudge(_searcher_nudge_queue)
        nudge(_embedder_nudge_queue)
        return enricher_instance.next_retry_delay()

    # Process queue commands. When a pass left rows waiting on an
    # unavailable service, the loop wakes on that service's cooldown instead
    # of idling until the indexer's next scan — the scan interval is for
    # finding new files, not for retrying an outage.
    retry_at: Optional[float] = None
    while True:
        try:
            now = time.monotonic()
            timeout = 30.0
            if retry_at is not None:
                timeout = max(0.5, min(timeout, retry_at - now))

            # Check for commands with timeout
            try:
                command = await asyncio.wait_for(
                    _enricher_queue.get(), timeout=timeout
                )

                logger.debug("Received command from queue: %s", command)

                if command == "stop":
                    logger.info("Stopping enricher task")
                    _shutdown_event.set()
                    break
                elif command == "enrich":
                    logger.debug("Manual enrichment triggered")
                    delay = await run_pass()
                    retry_at = None if delay is None else time.monotonic() + delay

            except asyncio.TimeoutError:
                if retry_at is not None and time.monotonic() >= retry_at:
                    logger.info(
                        "Retrying rows that were waiting on an unavailable "
                        "service"
                    )
                    delay = await run_pass()
                    retry_at = None if delay is None else time.monotonic() + delay
                else:
                    # No command in the timeout window; loop and wait again.
                    logger.debug(
                        "No enricher commands received; continuing to wait"
                    )
                continue

            # Small delay to avoid busy waiting
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            logger.info("Enricher worker cancelled.")
            break
        except Exception as e:
            logger.exception(f"Error in enricher worker: {str(e)}")
            await asyncio.sleep(5)  # Sleep longer on errors


def start_enricher(config, db_manager: AsyncEnricherDb) -> Optional[asyncio.Task]:
    """Start the enricher process in a background task"""
    global _enricher_task

    def log_task_result(task):
        try:
            task.result()  # Will raise if task failed
        except Exception as e:
            logger.exception(f"Enricher error: {e}")

    if _enricher_task and not _enricher_task.done():
        logger.warning("Enricher task already running, not starting another")
        return _enricher_task

    _enricher_task = asyncio.create_task(_enricher_worker(config, db_manager))
    _enricher_task.add_done_callback(log_task_result)

    logger.info("Started metadata enricher background task")
    return _enricher_task


async def stop_enricher() -> bool:
    """Stop the enricher task"""
    global _enricher_task
    if _enricher_task and not _enricher_task.done():
        logger.info("Sending stop command to enricher task")
        try:
            if _enricher_queue is None:
                logger.error("Enricher queue is not initialized; cannot send stop")
                return False
            _enricher_queue.put_nowait("stop")

            await asyncio.wait_for(_enricher_task, timeout=10.0)
            return True
        except asyncio.TimeoutError:
            logger.warning("Enricher task did not stop in time, cancelling...")
            _enricher_task.cancel()
            try:
                await asyncio.wait_for(_enricher_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            return True
        except Exception as e:
            logger.exception(f"Error stopping enricher task: {str(e)}")
            return False
    elif _enricher_task and _enricher_task.done():
        logger.info("Enricher task was already done.")
        return True
    logger.info("No active enricher task to stop.")
    return False


# Process orchestration lives in librarian.py, which runs this enricher's
# worker loop and the indexer's in one event loop (Phase 2b). start_enricher /
# stop_enricher above are its API.
