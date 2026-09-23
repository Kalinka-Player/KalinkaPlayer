import asyncio
import logging
import math
import time
import json
import shutil
import subprocess
import threading
import requests
from typing import Dict, Optional, List, Tuple

from ..config_model import LocalFilesConfig
from ..resolution.resolver import GUESSED
from ..storage import build_resolver
from ..utils.name_utils import clean_display_name
from ..utils.tracklist_names import name_carries_title
from .enricher_plugin import (
    EnricherPlugin,
    TransientEnrichmentError,
    verified_claims,
    raise_if_service_unavailable,
)
from .id_generator import generate_artist_id
from .match_utils import duration_bonus


logger = logging.getLogger(__name__.split(".")[-1])

# Without one the lookup blocks its worker thread for as long as the service
# stays silent; the read half is generous because a compressed fingerprint
# lookup is real work for the server.
_LOOKUP_TIMEOUT_S = (5, 30)

#: Artist ids that name nobody, so filling one contests nothing.
_UNNAMED_ARTIST_IDS = (None, "", "unknown_artist")


class AcoustIdPlugin(EnricherPlugin):
    """AcoustID audio fingerprinting plugin for track identification"""

    # 2: rescue-mode gating + fill-only writes.
    # 3: the rescue keys on provenance rather than emptiness, its match is
    # verified rather than inferred (see enrich_track), and an album that
    # names its own tracks is left to name them.
    ENRICHER_VERSION = 3

    def __init__(self, config: LocalFilesConfig, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.api_key = config.enricher.plugins.acoustid.api_key
        self.storage = build_resolver(config)
        # Probed once: a missing binary is a property of the installation, not
        # of the track, and shelling out per file turned one broken install
        # into one error line per file in the library.
        self.fpcalc = shutil.which("fpcalc")

        if not self.api_key:
            logger.warning("AcoustID API key not configured. Plugin will be disabled.")
        elif not self.fpcalc:
            logger.error(
                "AcoustID is configured but fpcalc (chromaprint) is missing, so "
                "fingerprint identification is off. Install chromaprint "
                "(Debian: libchromaprint-tools) and restart; the tracks it could "
                "not identify are retried by themselves."
            )

        # Rate limiting. The interval is enforced inside worker threads, so
        # the pacing state needs a real lock once several tracks are enriched
        # at once — without it concurrent lookups read the same
        # ``last_request_time`` and all fire together.
        self.last_request_time = 0
        self.request_interval = (
            1.0 / 3
        )  # 3 request per second to respect AcoustID limits
        self._rate_limit_lock = threading.Lock()

        # fpcalc decodes the whole file; a handful in parallel would saturate
        # a Pi's CPU and disk and starve everything else in the pass.
        self._fingerprint_slots = asyncio.Semaphore(2)

        # Match confidence thresholds (0-1.0)
        self.min_score_threshold = 0.7  # Minimum score to consider a match valid

    def config_signature(self) -> Dict:
        # Without a key every lookup short-circuits, so the plugin is
        # effectively inert; the moment a key is configured it can start
        # resolving tracks that previously FAILED. Presence is what
        # changes outcomes — the key value itself isn't recorded (no
        # secret in the fingerprint).
        #
        # fpcalc counts for the same reason, and the plugin stays loaded
        # without it: installing chromaprint has to move the fingerprint or
        # every track it failed would stay FAILED until something else did.
        return {
            "api_key_present": bool(self.api_key),
            "fpcalc_present": bool(self.fpcalc),
        }

    def _wait_for_rate_limit(self):
        """Wait to respect rate limits.

        Holds the lock across the sleep so concurrent lookups queue rather
        than all observing the same idle gap and firing at once.
        """
        with self._rate_limit_lock:
            now = time.time()
            elapsed = now - self.last_request_time

            if elapsed < self.request_interval:
                sleep_time = self.request_interval - elapsed
                time.sleep(sleep_time)

            self.last_request_time = time.time()

    def _generate_fingerprint(
        self, file_path: str
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Generate audio fingerprint using chromaprint (fpcalc)

        ``fpcalc`` opens a file by name and reports the duration it decoded,
        neither of which survives being handed a pipe, so a track on storage
        this process cannot hand to another one is copied out for the length
        of the call.

        Returns:
            Tuple of (fingerprint, duration)
        """
        storage = self.storage.for_path(file_path)
        try:
            if not storage.is_file(file_path):
                logger.error(f"File not found: {file_path}")
                return None, None

            with storage.materialize(file_path) as readable_path:
                result = self._run_fpcalc(readable_path)
            if result is None:
                return None, None

            # Parse JSON output
            data = json.loads(result.stdout)
            if "fingerprint" in data and "duration" in data:
                return data["fingerprint"], data["duration"]
            else:
                logger.error(
                    f"Missing fingerprint or duration in fpcalc output for {file_path}"
                )
                return None, None

        except OSError as e:
            # A track that left between the check above and the read of it —
            # a share drops one mid-pass — which says nothing about fpcalc.
            logger.error(f"Could not read {file_path} to fingerprint it: {e}")
            return None, None
        except subprocess.SubprocessError as e:
            logger.error(f"Error running fpcalc on {file_path}: {str(e)}")
            return None, None
        except json.JSONDecodeError:
            logger.error(
                f"fpcalc produced no usable output for {file_path} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
            return None, None
        except Exception as e:
            logger.error(
                f"Unexpected error generating fingerprint for {file_path}: {str(e)}"
            )
            return None, None

    def _run_fpcalc(self, readable_path: str) -> Optional[subprocess.CompletedProcess]:
        """What fpcalc printed for a file on this filesystem, or None when the
        binary has gone since it was probed at startup.

        Its own method so that the ``FileNotFoundError`` a missing binary
        raises is never mistaken for the one a missing track raises: they
        arrive at the same handler and mean opposite things.
        """
        cmd = [self.fpcalc or "fpcalc", "-json", readable_path]
        logger.debug(f"Running command: {' '.join(cmd)}")
        try:
            # A damaged file makes fpcalc exit non-zero after it has already
            # printed the fingerprint it built, and that is worth keeping.
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,  # Timeout after 30 seconds
            )
        except FileNotFoundError:
            logger.error("fpcalc (chromaprint) not found. Please install chromaprint.")
            return None
        except OSError as e:
            # Present but unrunnable — the wrong architecture, or not
            # executable. Named here so the caller's handler is left meaning
            # one thing: the track could not be read.
            logger.error(f"Could not run fpcalc: {e}")
            return None

    def _lookup_fingerprint(
        self, fingerprint: str, duration: int
    ) -> Optional[List[Dict]]:
        """
        Look up a fingerprint in the AcoustID database

        Returns:
            List of matching recordings with MusicBrainz IDs
        """
        if not self.api_key:
            logger.error("Cannot lookup fingerprint: AcoustID API key not configured")
            return None

        try:
            self._wait_for_rate_limit()

            # Prepare API request
            url = "https://api.acoustid.org/v2/lookup"
            params = {
                "client": self.api_key,
                "meta": "recordings releases releasegroups tracks compress",
                "fingerprint": fingerprint,
                "duration": math.floor(duration),
            }

            # POSTed: a URL, key and all, turns up in exceptions and debug logs.
            response = requests.post(url, data=params, timeout=_LOOKUP_TIMEOUT_S)
            raise_if_service_unavailable(response.status_code, "AcoustID")
            if response.status_code != 200:
                logger.error(
                    f"AcoustID API error: {response.status_code} - {response.text}"
                )
                return None

            data = response.json()
            if data.get("status") != "ok":
                logger.error(
                    f"AcoustID lookup failed: {data.get('error', 'Unknown error')}"
                )
                return None

            # Process and return results
            results = data.get("results", [])
            if not results:
                logger.debug("No AcoustID matches found")
                return None

            # Filter results by score
            valid_results = [
                r for r in results if r.get("score", 0) >= self.min_score_threshold
            ]
            if not valid_results:
                logger.debug(
                    f"No AcoustID matches with score >= {self.min_score_threshold}"
                )
                return None

            return valid_results

        except TransientEnrichmentError:
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientEnrichmentError(f"AcoustID is unreachable: {e}") from e
        except requests.RequestException as e:
            logger.error(f"AcoustID API request error: {str(e)}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing AcoustID API response: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during AcoustID lookup: {str(e)}")
            return None

    # When two candidate recordings end up with nearly-tied combined
    # scores, we can't reliably tell them apart. Leave the track as
    # orphan rather than commit to the wrong release — the artist view
    # still surfaces orphan tracks, and re-enrichment may resolve later.
    MIN_MATCH_MARGIN = 5.0

    @staticmethod
    def _release_group_score(rg_type, secondary_types) -> float:
        """Heuristic score for a release group by its type metadata.

        Prefer a clean 'Album' release; deprioritize compilations, live
        cuts, soundtracks, and other secondary placements. The same
        recording usually appears on many releases — the original studio
        album, a greatest-hits compilation, a regional re-issue, a
        live recording — and the compilations/lives are almost never
        what the user has on disk, so we lean hard against them.
        """
        primary = (rg_type or "").lower()
        secondary = [s.lower() for s in (secondary_types or [])]

        score = 0.0
        if primary == "album":
            score += 30
        elif primary == "ep":
            score += 20
        elif primary == "single":
            score += 15
        elif primary == "broadcast":
            score += 5

        for sec in secondary:
            if sec in ("compilation", "live", "remix"):
                score -= 15
            elif sec in ("soundtrack", "demo", "interview", "spokenword", "audiobook"):
                score -= 25

        return score

    @staticmethod
    def _position_match_score(
        release: Dict,
        recording_mbid: Optional[str],
        track_number: Optional[int],
        disc_number: Optional[int],
    ) -> float:
        """Bonus when this release's tracklist places the recording at
        the same position the local file claims via its ID3 tags.

        AcoustID with ``meta=tracks`` returns
        ``release.mediums[].tracks[]`` carrying each track's recording
        MBID, its position on the medium, and the medium's own position
        (disc number). If our local file is tagged "track 5 disc 1" and
        a candidate release places this recording at position 5/disc 1,
        that's a strong signal it's the right one — particularly useful
        for distinguishing rip-with-bonus-track editions from the
        original.
        """
        if not recording_mbid or track_number is None:
            return 0.0
        try:
            target_t = int(track_number)
        except (TypeError, ValueError):
            return 0.0
        target_d: Optional[int] = None
        if disc_number is not None:
            try:
                target_d = int(disc_number)
            except (TypeError, ValueError):
                target_d = None

        for medium in release.get("mediums") or []:
            for track in medium.get("tracks") or []:
                if track.get("id") != recording_mbid:
                    continue
                score = 0.0
                try:
                    if int(track.get("position")) == target_t:
                        score += 20.0
                except (TypeError, ValueError):
                    pass
                if target_d is not None:
                    try:
                        if int(medium.get("position")) == target_d:
                            score += 10.0
                    except (TypeError, ValueError):
                        pass
                return score
        return 0.0

    # Bonus added when a candidate release matches the local album's
    # already-known MBID. Large enough to outweigh any release-group
    # / position signal because matching the local album is by far the
    # strongest signal we have for track→album consistency.
    LOCAL_ALBUM_MATCH_BONUS = 1000.0

    async def _lookup_local_album_mbid(
        self, album_id: Optional[str]
    ) -> Optional[str]:
        """Fetch the MBID stored on the local album row, if any.

        Returns None for the ``unknown_album`` sentinel, for a missing
        row, or when the album hasn't been enriched yet — callers
        should treat None as "no context available".
        """
        if not album_id or album_id == "unknown_album":
            return None
        album = await self.db_manager.get_album_by_id(album_id)
        if not album:
            return None
        mbid = album.get("mbid")
        return mbid if mbid else None

    @staticmethod
    def _recording_has_release(recording: Dict, release_mbid: str) -> bool:
        """Return True if ``recording`` lists a release with this MBID.

        Walks both the ``releasegroups[*].releases[*]`` tree (present
        when AcoustID was queried with ``meta=releasegroups``) and the
        flat ``releases`` list as a fallback.
        """
        for rg in recording.get("releasegroups") or []:
            for rel in rg.get("releases") or []:
                if rel.get("id") == release_mbid:
                    return True
        for rel in recording.get("releases") or []:
            if rel.get("id") == release_mbid:
                return True
        return False

    def _pick_best_release(
        self,
        recording: Dict,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
        local_album_mbid: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Pick the best (album_title, album_mbid) for a recording.

        AcoustID with ``meta=releasegroups`` returns releases nested
        under their release groups with a ``type`` (and
        ``secondarytypes``) field, which lets us prefer the original
        studio album over a compilation re-release of the same
        recording. When the local file is tagged with a track/disc
        number, releases whose tracklist puts the recording at the same
        position get a further bonus on top of the release-group score.
        Falls back to ``recording.releases`` when no release-group
        metadata is present.

        When ``local_album_mbid`` is provided (the local album has
        already been enriched to a specific MB release), a release
        whose MBID matches gets ``LOCAL_ALBUM_MATCH_BONUS`` so the
        recording's release defaults to that one — keeping the track's
        chosen release in sync with the album row's release.
        """
        recording_mbid = recording.get("id")
        candidates: List[Tuple[float, Dict]] = []

        for rg in recording.get("releasegroups") or []:
            rg_score = self._release_group_score(
                rg.get("type"), rg.get("secondarytypes")
            )
            for rel in rg.get("releases") or []:
                pos_score = self._position_match_score(
                    rel, recording_mbid, track_number, disc_number
                )
                ctx_bonus = (
                    self.LOCAL_ALBUM_MATCH_BONUS
                    if local_album_mbid and rel.get("id") == local_album_mbid
                    else 0.0
                )
                candidates.append((rg_score + pos_score + ctx_bonus, rel))

        if not candidates:
            for rel in recording.get("releases") or []:
                pos_score = self._position_match_score(
                    rel, recording_mbid, track_number, disc_number
                )
                ctx_bonus = (
                    self.LOCAL_ALBUM_MATCH_BONUS
                    if local_album_mbid and rel.get("id") == local_album_mbid
                    else 0.0
                )
                candidates.append((pos_score + ctx_bonus, rel))

        if not candidates:
            return None, None

        # Highest-scoring release wins; first occurrence breaks ties so
        # behavior reduces to ``releases[0]`` when no type info exists.
        best = max(range(len(candidates)), key=lambda i: candidates[i][0])
        release = candidates[best][1]
        return release.get("title"), release.get("id")

    def _get_best_match_info(
        self,
        acoustid_results: List[Dict],
        target_duration_s: Optional[float] = None,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
        local_album_mbid: Optional[str] = None,
    ) -> Optional[Dict]:
        """Extract the best match information from AcoustID results.

        AcoustID typically returns several results, each potentially
        containing several recordings. Picking ``results[0].recordings[0]``
        blindly (the prior behavior) often grabs a re-issue or compilation
        version of a track whose original recording is also in the
        candidate list. We instead score every (result, recording) pair
        by combined AcoustID confidence + duration match against the
        local file, pick the highest, and reject the pick if the next
        candidate is within ``MIN_MATCH_MARGIN`` — the latter prevents
        coin-flip assignments to the wrong recording when several
        candidates look equally plausible.

        When ``local_album_mbid`` is provided, recordings whose
        ``releasegroups[*].releases[*]`` (or fallback ``releases``)
        include a release matching that MBID get a large additive
        bonus. This is the album-context re-rank: if the local album
        is already enriched to a specific MB release, the chosen
        recording should also be on that release rather than on some
        compilation re-issue with a coincidentally-similar duration.
        """
        if not acoustid_results:
            return None

        best_pair: Optional[Tuple[Dict, Dict]] = None
        best_combined = float("-inf")
        runner_up_combined: float = float("-inf")
        best_score = 0.0

        for result in acoustid_results:
            base_score = float(result.get("score", 0)) * 100  # 0-100 scale
            for recording in result.get("recordings", []) or []:
                if not recording.get("id"):
                    continue
                # AcoustID recording duration is in seconds.
                cand_s = recording.get("duration")
                combined = base_score + duration_bonus(target_duration_s, cand_s)
                if local_album_mbid and self._recording_has_release(
                    recording, local_album_mbid
                ):
                    combined += self.LOCAL_ALBUM_MATCH_BONUS
                if combined > best_combined:
                    runner_up_combined = best_combined
                    best_combined = combined
                    best_pair = (result, recording)
                    best_score = result.get("score", 0)
                elif combined > runner_up_combined:
                    runner_up_combined = combined

        if best_pair is None:
            logger.debug("No recordings with MBIDs in AcoustID results")
            return None

        # Skip when the top two candidates are effectively tied.
        if (
            runner_up_combined != float("-inf")
            and (best_combined - runner_up_combined) < self.MIN_MATCH_MARGIN
        ):
            logger.info(
                "AcoustID match is ambiguous: best=%.1f runner_up=%.1f "
                "margin<%.1f — holding as orphan rather than committing",
                best_combined,
                runner_up_combined,
                self.MIN_MATCH_MARGIN,
            )
            return None

        best_result, best_recording = best_pair

        mb_recording_id = best_recording.get("id")
        title = best_recording.get("title")

        artists = best_recording.get("artists", []) or []
        artist_name = artists[0].get("name") if artists else None
        artist_id = artists[0].get("id") if artists else None

        album_title, album_id = self._pick_best_release(
            best_recording, track_number, disc_number, local_album_mbid
        )

        return {
            "score": best_score,
            "recording_mbid": mb_recording_id,
            "title": title,
            "artist_name": artist_name,
            "artist_mbid": artist_id,
            "album_title": album_title,
            "album_mbid": album_id,
        }

    async def _create_or_get_artist(
        self, artist_name: str, artist_mbid: Optional[str] = None
    ) -> Optional[str]:
        """
        Find artist by name or MBID, or create if not exists

        Returns:
            Artist ID
        """
        artist_name = clean_display_name(artist_name) if artist_name else ""
        if not artist_name:
            return None

        # Try to find existing artist by MBID first (most accurate)
        if artist_mbid:
            existing_artist = await self.db_manager.get_artist_by_mbid(artist_mbid)
            if existing_artist:
                return existing_artist["id"]

        # Try to find by name
        artists, _ = await self.db_manager.search_artists(artist_name, limit=1)
        # Check for exact match
        existing_artist = next(
            (a for a in artists if a["name"].lower() == artist_name.lower()), None
        )
        if existing_artist:
            # If found and we have an MBID but they don't, update it
            if artist_mbid and not existing_artist.get("mbid"):
                await self.db_manager.update_artist(
                    existing_artist["id"], {"mbid": artist_mbid}
                )
            return existing_artist["id"]

        # Create new artist
        artist_data = {
            "name": artist_name,
            "mbid": artist_mbid,
        }

        artist_id = generate_artist_id(artist_name)
        artist_data["id"] = artist_id
        artist_data["last_updated"] = int(time.time())

        await self.db_manager.insert_artist(artist_data)
        return artist_data["id"]


    def can_enrich_artist(self) -> bool:
        return False  # This plugin doesn't directly enrich artists, only via track identification

    def can_enrich_album(self) -> bool:
        return False  # This plugin doesn't directly enrich albums, only via track identification

    def can_enrich_track(self) -> bool:
        # fpcalc is as hard a requirement as the key: there is no fingerprint
        # to look up without it, so the chain must skip this plugin rather
        # than have every track discover the same missing binary.
        return bool(self.api_key and self.fpcalc)

    async def enrich_artist(self, artist: Dict) -> Optional[Dict]:
        """
        AcoustID doesn't directly enrich artists.
        This is a placeholder to satisfy the abstract method requirement.
        """
        return None

    async def enrich_album(self, album: Dict) -> Optional[Dict]:
        """
        AcoustID doesn't directly enrich albums.
        This is a placeholder to satisfy the abstract method requirement.
        """
        return None

    async def _is_a_guess(self, entity_type: str, entity_id: str, field: str) -> bool:
        """Whether the file's own path is all that ever supplied this value."""
        origin = await self.db_manager.get_resolved_origin(
            entity_type, entity_id, field
        )
        return bool(origin) and origin["tier"] == GUESSED

    async def _artist_is_unresolved(self, track: Dict) -> bool:
        """True when nothing better than the file's own path named the artist."""
        artist_id = track.get("artist_id")
        return artist_id in _UNNAMED_ARTIST_IDS or await self._is_a_guess(
            "artist", artist_id, "name"
        )

    async def _named_by_its_file_name(self, track: Dict) -> bool:
        """Whether the file's name is this track's title on its album.

        A rip named by hand holds its album's titles in the file names, and
        those belong to the pressing on the shelf. The umbrella album is
        unrelated loose tracks, so it has no tracklist to keep whole.
        """
        album_id = track.get("album_id")
        if not album_id or album_id == "unknown_album":
            return False
        return name_carries_title(
            track.get("file_path") or "",
            await self.db_manager.get_album_track_paths(album_id),
        )

    async def _title_is_unresolved(self, track: Dict) -> bool:
        """True when nothing better than the file's own path named the track."""
        return not (track.get("title") or "").strip() or await self._is_a_guess(
            "track", track["id"], "title"
        )

    async def enrich_track(self, track: Dict) -> Optional[Dict]:
        """
        Enrich track using audio fingerprinting (rescue mode, §6.3).

        This will:
        1. Skip a track an external source already identified *and* a tag
           already named — see the gate below
        2. Generate fingerprint from audio file
        3. Look up fingerprint in AcoustID
        4. Record the recording mbid and fill only the unresolved field(s) —
           never overwrite a tagged title, repoint a known artist, or re-title
           an album that names its own tracks
        """
        try:
            # Skip if track is already enriched or no file path
            if track.get("enriched") or not track.get("file_path"):
                return None

            # Rescue mode (§6.3): the audio is consulted when no external
            # source identified the track, or when the only thing naming it is
            # its own path. The latter matters because the text search that
            # found the mbid was built *from* that path — a match on a guess
            # proves nothing the audio cannot overturn. A missing *album* never
            # triggers a lookup: album identity is the clustering pass's job.
            if await self._named_by_its_file_name(track):
                # Only part of an album is ever identifiable, so re-titling
                # that part would leave the rest in a different naming. A
                # blank is still filled: it completes the tracklist.
                title_unresolved = not (track.get("title") or "").strip()
                artist_unresolved = track.get("artist_id") in _UNNAMED_ARTIST_IDS
            else:
                artist_unresolved = await self._artist_is_unresolved(track)
                title_unresolved = await self._title_is_unresolved(track)
            if track.get("mbid") and not (artist_unresolved or title_unresolved):
                logger.debug(
                    f"Skipping acoustid enrichment for track {track.get('title', 'Unknown')} - "
                    f"already identified, and named by more than its path"
                )
                return None

            logger.debug(
                f"Fingerprinting track: {track.get('title', 'Unknown')} - {track.get('file_path')}"
            )

            # Generate fingerprint. ``_generate_fingerprint`` (fpcalc
            # subprocess) and ``_lookup_fingerprint`` (HTTP + rate-limit
            # sleep) are blocking, so run them off the event loop to avoid
            # stalling the enricher's other coroutines.
            async with self._fingerprint_slots:
                fingerprint, duration = await asyncio.to_thread(
                    self._generate_fingerprint, track["file_path"]
                )
            if not fingerprint or not duration:
                logger.debug(
                    f"Could not generate fingerprint for {track.get('file_path')}"
                )
                return None

            # Persist the (expensive) chromaprint for reuse. Best-effort: a
            # write failure must not abort the lookup.
            try:
                await self.db_manager.save_fingerprint(track["id"], fingerprint)
            except Exception as e:
                logger.debug("Could not persist fingerprint: %s", e)

            # Look up fingerprint
            results = await asyncio.to_thread(
                self._lookup_fingerprint, fingerprint, duration
            )
            if not results:
                logger.debug(f"No AcoustID matches for {track.get('file_path')}")
                return None

            # Extract best match information. Pass the local file's
            # duration (preferring the tag-derived value when present,
            # falling back to the fpcalc-measured one) so duration is used
            # to pick among same-named recordings, and the file's
            # track/disc numbers so a release that places this recording
            # at the same position gets preference over one that doesn't.
            # Also pass the local album's MBID (if its row has already
            # been enriched) so a recording on the same release outranks
            # one on a compilation re-issue.
            target_duration_s = track.get("duration") or duration
            local_album_mbid = await self._lookup_local_album_mbid(
                track.get("album_id")
            )
            match_info = self._get_best_match_info(
                results,
                target_duration_s,
                track_number=track.get("track_number"),
                disc_number=track.get("disc_number"),
                local_album_mbid=local_album_mbid,
            )
            if not match_info:
                logger.debug(
                    f"Could not extract match info for {track.get('file_path')}"
                )
                return None

            logger.info(
                f"AcoustID match found for {track.get('file_path')} - "
                f"Score: {match_info['score']:.2f}, "
                f"Title: {match_info.get('title')}, "
                f"Artist: {match_info.get('artist_name')}"
            )

            # The recording mbid is the rescue result and always recorded;
            # everything else is fill-only (§6.3).
            updates = {
                "mbid": match_info["recording_mbid"],
                "match_score": int(match_info["score"] * 100),  # 0-100 scale
            }

            # Title is a resolvable display field, so it's a claim, never a
            # direct write (resolution is the sole writer). Emitted only when
            # nothing better than the path named the track, which is why it is
            # verified: a fingerprint identifies the recording itself, so it
            # outranks both the guess and the text match built on it. A tagged
            # title yields no claim at all and is never contested.
            claims = verified_claims(
                f"acoustid:{match_info['recording_mbid']}",
                {"title": match_info.get("title") if title_unresolved else None},
            )

            changed_items = {"artists": set(), "albums": set()}

            # Fill the artist only when unresolved; never move a track off one
            # a tag or an external source named.
            if artist_unresolved and match_info.get("artist_name"):
                original_artist_id = track.get("artist_id")
                artist_id = await self._create_or_get_artist(
                    match_info["artist_name"], match_info.get("artist_mbid")
                )
                if artist_id:
                    updates["artist_id"] = artist_id
                    updates["artist_name"] = match_info["artist_name"]
                    if artist_id != original_artist_id:
                        changed_items["artists"].add(artist_id)

            # Album membership stays with the clustering pass (§3); AcoustID
            # never creates albums or moves a track (powers removed in 1f).
            result = {"updates": updates, "claims": claims}

            # If we have any items that need further enrichment, add them to result
            if any(changed_items.values()):
                result["changed_items"] = {
                    "artists": list(changed_items["artists"]),
                    "albums": list(changed_items["albums"]),
                    "tracks": [track["id"]],
                }

            # Return the updates for this track
            return result

        except TransientEnrichmentError:
            raise
        except Exception as e:
            logger.error(
                f"Error enriching track {track.get('title', 'Unknown')} with AcoustID: {str(e)}"
            )
            return None
