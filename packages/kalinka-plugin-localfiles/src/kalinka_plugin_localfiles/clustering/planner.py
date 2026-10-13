"""Turn real track + evidence rows into a per-folder cluster plan.

Read-only: builds ``TrackFeatures`` from the display row plus its
``track_evidence``, runs the partition engine, and classifies each resulting
group (album / compilation / singles_pool). It proposes assignments; applying
them (album_id reassignment, album_cluster writes) is a later step.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..resolution.resolver import FOLDER_NAME, TAG_CONSENSUS
from ..utils.name_utils import (
    clean_display_name,
    normalize_for_id,
    repair_tag_text,
)
from .classify import (
    CONTAINER_FOLDER_RE,
    VARIOUS_ARTISTS_ID,
    compilation_title,
    is_declared_va_folder,
    is_va_folder,
    normalize_album_title,
    strip_artist_prefix,
    strip_disc_suffix,
)
from .engine import PartitionResult, partition_folder
from .signals import TrackFeatures

# Tag names for the same concept across ID3 (MP3) and Vorbis (FLAC).
_ALBUM_TAGS = ("album", "TALB")
_ALBUMARTIST_TAGS = ("albumartist", "album artist", "TPE2")
_COMPILATION_TAGS = ("compilation", "TCMP")


def _loads(s: Optional[str]) -> Dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


def _tag(raw: Dict, *keys: str) -> Optional[str]:
    for k in keys:
        if k in raw:
            v = raw[k]
            v = v[0] if isinstance(v, list) else v
            if v not in (None, ""):
                return str(v)
    return None


def _album_display(raw: Dict, legacy_encoding: Optional[str] = None) -> Optional[str]:
    album = _tag(raw, *_ALBUM_TAGS)
    if not album:
        return None
    # Same repairs the indexer stores, so a re-cluster can never rewrite
    # a repaired album title back to its raw-tag form.
    return clean_display_name(repair_tag_text(album, legacy_encoding))


def _cue_album(cue: Dict) -> Optional[str]:
    """Disc title from a parsed cue sheet (single-file CD rip), if any."""
    album = cue.get("album") if isinstance(cue, dict) else None
    return clean_display_name(album) if album else None


def build_features(track: Dict, evidence: Optional[Dict]) -> TrackFeatures:
    """Assemble the scorer's per-track features from a track row + evidence."""
    evidence = evidence or {}
    raw = _loads(evidence.get("raw_tags"))
    stream = _loads(evidence.get("stream_info"))
    art_phash = evidence.get("art_phash")
    cue_sheet = evidence.get("cue_sheet")
    import_batch = evidence.get("import_batch")

    album_display = _album_display(raw)
    # Album key: normalized, disc-suffix stripped so "X (Disc 1)"/"X (Disc 2)"
    # share a bucket and are distinguished by disc_number within the cluster.
    album_key = None
    if album_display:
        album_key = normalize_for_id(strip_disc_suffix(album_display)) or None

    albumartist = _tag(raw, *_ALBUMARTIST_TAGS)
    albumartist_key = normalize_for_id(albumartist) if albumartist else None
    compilation = (_tag(raw, *_COMPILATION_TAGS) or "").strip() in ("1", "true", "True")

    stream_key = None
    if stream:
        stream_key = (
            f"{stream.get('codec')}|{stream.get('sample_rate')}|"
            f"{stream.get('bits_per_sample')}"
        )

    return TrackFeatures(
        track_id=track["id"],
        album_key=album_key,
        albumartist_key=albumartist_key,
        artist_key=track.get("artist_id"),
        track_number=track.get("track_number"),
        disc_number=track.get("disc_number"),
        art_phash=art_phash,
        stream_key=stream_key,
        compilation=compilation,
        cue_sheet=cue_sheet,
        import_batch=import_batch,
    )


@dataclass
class ClusterPlan:
    track_ids: List[str]
    kind: str                       # album | compilation | singles_pool | multi_disc
    title: str                      # "" for singles_pool (stays unknown_album)
    anchor_artist_id: str
    grouping_basis: Dict[str, object] = field(default_factory=dict)
    folder: str = ""
    disc_numbers: frozenset = field(default_factory=frozenset)
    albumartist_key: Optional[str] = None
    # Where ``title`` came from, so the caller can record its provenance: a
    # folder-derived one is a guess an external source may later correct.
    title_source: str = FOLDER_NAME
    # Release year read out of the folder, for an album no tag dated. It
    # travels with the title because it comes from the same reading of the
    # same folder name ("1979 - The Wall").
    year: Optional[int] = None


@dataclass
class FolderPlan:
    folder: str
    clusters: List[ClusterPlan]
    split: bool = False


def _dominant(values) -> Optional[str]:
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else None


def _repaired(text: Optional[str], legacy_encoding: Optional[str]) -> str:
    """Path text read as tag text.

    A folder name carries the same damage a tag does — mojibake and unspaced
    abbreviations ("В.Цой - Черный альбом") — and an album named after its
    folder that keeps them no longer matches its own repaired artist name when
    the enricher tries to strip the artist prefix.
    """
    if not text:
        return ""
    return clean_display_name(repair_tag_text(text, legacy_encoding))


def plan_folder(
    folder: str,
    rows: List[Tuple[Dict, Optional[Dict]]],
    legacy_encoding: Optional[str] = None,
    path_album_title: Optional[str] = None,
    path_album_year: Optional[int] = None,
    is_library_root: bool = False,
) -> FolderPlan:
    """Plan the clusters for one folder from (track_row, evidence_row) pairs.

    @param path_album_title What the filename model read as the album for this
        folder, if anything. It sits below the tags and above the bare folder
        name in the title ladder — the model drops a year prefix or an edition
        suffix that the basename keeps.
    @param path_album_year The year it read there, for an album no tag dated.
    """
    if not rows:
        return FolderPlan(folder=folder, clusters=[], split=False)
    features = [build_features(t, e) for t, e in rows]
    display_by_id = {
        t["id"]: (
            t,
            _album_display(_loads((e or {}).get("raw_tags")), legacy_encoding),
            _cue_album(_loads((e or {}).get("cue_tracks"))),
        )
        for t, e in rows
    }
    name_by_id = {
        t.get("artist_id"): t.get("artist_name")
        for t, _ in rows
        if t.get("artist_id") and t.get("artist_name")
    }

    # Folder-level V/A dump: a flat playlist folder (nearly every track a
    # different artist) with no shared album tag. The partition engine would
    # otherwise fragment it — tracks with their own cover art each split into a
    # single-artist album, art-less ones clump into V/A umbrella albums. The
    # V/A folder policy (detach to unknown_album so tracks surface as singles
    # under their real artist) is the right outcome for the folder's loose
    # tracks, so short-circuit before partitioning. A real compilation is
    # protected two ways: its tracks share an album tag (fails the
    # no-shared-album test), or the folder is an explicitly declared "VA - X"
    # compilation (exempted).
    #
    # Exemption inside a dump: a track that fully declares its release —
    # album AND albumartist tags, the deliberately-mastered signature that
    # playlist rips and junk tags lack — keeps its album. The V/A test
    # re-applied to just those tracks is the backstop: a curated pool of
    # mastered singles (many artists) still flattens whole, while a real
    # release lost in a junk pile survives. Strays bucket by album tag
    # directly, skipping the merge scorer — it would fuse unrelated singles
    # on weak positives like a shared codec.
    folder_real_artists = {
        t.get("artist_id")
        for t, _ in rows
        if t.get("artist_id") and t.get("artist_id") != "unknown_artist"
    }
    album_keys = [f.album_key for f in features if f.album_key]
    dominant_album_cov = (
        Counter(album_keys).most_common(1)[0][1] / len(rows) if album_keys else 0.0
    )
    clusters: List[ClusterPlan] = []
    # A storage container supplies no album identity. Its explicit albums
    # stay separate, and its loose files must not inherit either a neighbour's
    # tags or the name of the library root (e.g. "Music").
    container = is_library_root or bool(
        CONTAINER_FOLDER_RE.fullmatch(os.path.basename(folder.rstrip("/")))
    )
    if (
        is_va_folder(len(folder_real_artists), len(rows))
        and dominant_album_cov < 0.5
        and not is_declared_va_folder(folder)
    ):
        tagged = [f for f in features if f.album_key and f.albumartist_key]
        tagged_artists = {
            display_by_id[f.track_id][0].get("artist_id")
            for f in tagged
            if display_by_id[f.track_id][0].get("artist_id")
            and display_by_id[f.track_id][0].get("artist_id") != "unknown_artist"
        }
        keep_strays = bool(tagged) and not is_va_folder(
            len(tagged_artists), len(tagged)
        )
        stray_ids = {f.track_id for f in tagged}
        pool_ids = [
            f.track_id
            for f in features
            if not keep_strays or f.track_id not in stray_ids
        ]
        if pool_ids:
            clusters.append(
                ClusterPlan(
                    pool_ids,
                    "singles_pool",
                    "",
                    "unknown_artist",
                    {
                        "reason": "va_dump_folder",
                        "distinct_artists": len(folder_real_artists),
                        "tracks": len(pool_ids),
                    },
                    folder,
                )
            )
        if not keep_strays:
            return FolderPlan(folder=folder, clusters=clusters, split=False)
        # Keyed by (album, albumartist): a generic album tag ("Greatest
        # Hits") must not fuse two artists' strays.
        by_key: Dict[Tuple[str, str], List[TrackFeatures]] = {}
        for f in tagged:
            by_key.setdefault((f.album_key, f.albumartist_key), []).append(f)
        result = PartitionResult(
            groups=list(by_key.values()),
            split=True,
            basis={"reason": "va_dump_tagged_stray"},
        )
    elif container:
        buckets = {}
        for feature in features:
            _, album, cue_album = display_by_id[feature.track_id]
            key = (
                normalize_for_id(strip_disc_suffix(album or cue_album or "")),
                feature.albumartist_key,
            )
            # Artist guesses cannot partition loose files into pretend albums.
            if not key[0]:
                key = ("", None)
            buckets.setdefault(key, []).append(feature)
        result = PartitionResult(
            groups=list(buckets.values()), split=len(buckets) > 1,
            basis={"reason": "library_container"},
        )
    else:
        result = partition_folder(features)
    for group in result.groups:
        ids = [f.track_id for f in group]
        member_tracks = [display_by_id[i][0] for i in ids]
        real_artists = {
            t.get("artist_id")
            for t in member_tracks
            if t.get("artist_id") and t.get("artist_id") != "unknown_artist"
        }
        discs = frozenset(f.disc_number for f in group if f.disc_number)
        aa_key = _dominant(f.albumartist_key for f in group)

        titles = [display_by_id[i][1] for i in ids]
        cue_titles = [display_by_id[i][2] for i in ids]
        tagged = _dominant(titles) or _dominant(cue_titles)
        if container and not tagged:
            clusters.append(
                ClusterPlan(ids, "singles_pool", "", "unknown_artist",
                            {"reason": "library_container"}, folder, discs, aa_key)
            )
            continue

        if not container and is_va_folder(len(real_artists), len(ids)):
            comp = compilation_title(folder)
            if comp is None:
                # A generic dump — leave tracks loose (unknown_album), no album.
                clusters.append(
                    ClusterPlan(ids, "singles_pool", "", "unknown_artist",
                                {"reason": "generic_dump"}, folder, discs, aa_key)
                )
                continue
            clusters.append(
                ClusterPlan(ids, "compilation", normalize_album_title(comp),
                            VARIOUS_ARTISTS_ID, {"reason": "va_compilation"},
                            folder, discs, aa_key)
            )
            continue

        title = (
            tagged
            or _repaired(path_album_title, legacy_encoding)
            or _repaired(os.path.basename(folder.rstrip("/")), legacy_encoding)
            or ""
        )
        title_source = TAG_CONSENSUS if tagged else FOLDER_NAME
        anchor = _dominant(t.get("artist_id") for t in member_tracks) or "unknown_artist"

        # Multi-disc-in-one-folder: if the members' titles differ *only* by a
        # disc suffix (e.g. "… (Disc 1)" / "… (Disc 2)"), it is one album — drop
        # the suffix and mark it multi_disc. A standalone "… Vol. 2" (a single
        # title variant) is left intact, so real volumed releases keep their name.
        kind = "album"
        raw_titles = {t for t in titles if t}
        if len(raw_titles) >= 2 and len({strip_disc_suffix(t) for t in raw_titles}) == 1:
            title = strip_disc_suffix(title)
            kind = "multi_disc"
        title = normalize_album_title(title)
        # Drop a leading "Artist - " a folder name leaves on the album title
        # ("The Beatles - Abbey Road" -> "Abbey Road"). Guarded against
        # eponymous albums by strip_artist_prefix.
        anchor_name = name_by_id.get(anchor)
        if anchor_name:
            title = strip_artist_prefix(title, anchor_name)

        clusters.append(
            ClusterPlan(ids, kind, title, anchor, dict(result.basis),
                        folder, discs, aa_key, title_source, path_album_year)
        )

    return FolderPlan(folder=folder, clusters=clusters, split=result.split)
