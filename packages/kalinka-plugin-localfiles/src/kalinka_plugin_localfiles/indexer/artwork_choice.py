"""Which cover an album should have, decided from what the index knows.

Pure: no file is opened here. The resolver gathers the album's tracks, the
folder images it may use and what was decided before, and asks.

Precedence: an image a folder belonging to the album names as its cover,
then the embedded front cover most of its tracks carry, then the most common
untyped embedded picture, then an image chosen by its look, and otherwise
nothing — the album's cover is then left to the online sources.
"""

from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from ..clustering.signals import ART_DIFF_MIN_HAMMING, ART_SAME_MAX_HAMMING
from ..utils.name_utils import album_folder_for_path

FOLDER_ART, EMBEDDED_ART, NO_ART = "folder_art", "embedded_art", "none"

#: Picture types as ID3 and FLAC number them.
FRONT_COVER, OTHER_PICTURE = 3, 0

_DIGITS = re.compile(r"(\d+)")


def natural_key(path: str) -> Tuple:
    """Orders ``CD2`` before ``CD10``."""
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _DIGITS.split(path)
    )


def embedded_key(phash: str) -> str:
    """Names an embedded picture as a decision, whichever track it is read
    from."""
    return f"{EMBEDDED_ART}:{phash}"


class Member(NamedTuple):
    """One of an album's tracks, as far as its cover is concerned."""

    track_id: str
    path: str
    disc: Optional[int]
    track: Optional[int]
    phash: Optional[str]
    #: 3 front cover, 0 other, None when read before types were recorded —
    #: then the reader had already preferred the front, so it counts as one.
    art_type: Optional[int]


def track_order(member: Member) -> Tuple:
    """Disc, track, then path: never the order a scan happened to read in."""
    return (
        member.disc if member.disc is not None else 1,
        member.track if member.track is not None else math.inf,
        natural_key(member.path),
    )


class EmbeddedChoice(NamedTuple):
    """One picture the album's files embed, and the tracks carrying it."""

    phash: str
    picture_type: Optional[int]
    members: List[Member]
    reason: str


def embedded_choices(members: Iterable[Member]) -> List[EmbeddedChoice]:
    """The album's embedded pictures, most fitting first.

    Front covers come before untyped pictures; among either, the one most
    tracks carry, ties going to the earliest track. Pictures are told apart
    by their hash, so one cover re-encoded per track still counts once.
    """
    groups: Dict[bool, List[List[Member]]] = defaultdict(list)
    for member in sorted(members, key=track_order):
        if member.phash:
            front = member.art_type != OTHER_PICTURE
            _join(groups[front], member)
    ranked = sorted(
        ((front, group) for front, picked in groups.items() for group in picked),
        key=lambda item: (not item[0], -len(item[1]), track_order(item[1][0])),
    )
    return [
        EmbeddedChoice(
            group[0].phash,
            group[0].art_type,
            group,
            "most common embedded front cover"
            if front
            else "most common untyped embedded picture",
        )
        for front, group in ranked
    ]


def _join(groups: List[List[Member]], member: Member) -> None:
    """Add ``member`` to the group whose first picture it is, give or take
    re-encoding, or start one. Compared with the first only, so a run of
    near neighbours cannot chain two pictures into one."""
    for group in groups:
        if hamming(group[0].phash, member.phash) <= ART_SAME_MAX_HAMMING:
            group.append(member)
            return
    groups.append([member])


class FolderPick(NamedTuple):
    """A folder image the album may use, and the file as it was then."""

    path: str
    box: Optional[Tuple[float, float, float, float]]
    tier: str
    size: Optional[int]
    mtime_ns: Optional[int]

    @property
    def key(self) -> str:
        return f"{FOLDER_ART}:{self.path}:{self.size}:{self.mtime_ns}"


class Desired(NamedTuple):
    """The cover an album should have.

    ``key`` names it stably — the same folder file unchanged, or the same
    embedded picture whichever track it is read from — so a pass can tell
    the album already has it without opening anything.
    """

    source: str
    key: str
    reason: str
    folder: Optional[FolderPick] = None
    embedded: Optional[EmbeddedChoice] = None


def decide(
    named: Optional[FolderPick],
    embedded: Sequence[EmbeddedChoice],
    scan: Optional[FolderPick],
) -> Desired:
    """The cover an album should have, given the best folder image named as
    a cover, its embedded pictures (as :func:`embedded_choices` ranks them)
    and the best folder image chosen by its look."""
    if named is not None:
        return Desired(FOLDER_ART, named.key, "album folder cover", folder=named)
    if embedded:
        best = embedded[0]
        return Desired(EMBEDDED_ART, embedded_key(best.phash), best.reason, embedded=best)
    if scan is not None:
        return Desired(FOLDER_ART, scan.key, "album folder scan", folder=scan)
    return Desired(NO_ART, NO_ART, "no local artwork")


class AlbumFolders(NamedTuple):
    """The directories whose images may be taken as an album's cover.

    @param folders The album folders it alone has tracks in, best first.
    @param discs Disc subfolders (``CD1``) it alone has tracks directly in.
    """

    folders: List[str]
    discs: List[str]

    @property
    def roots(self) -> List[str]:
        return self.folders + self.discs


def album_folders(
    tracks: Iterable[Tuple[str, str]], library_roots: Iterable[str]
) -> Dict[str, AlbumFolders]:
    """Which folders belong to which album, from (album_id, path) pairs.

    A folder belongs to an album when every track in it — directly, or in
    its disc subfolders — is that album's. Several single-file albums in one
    directory, or loose tracks beside an album, leave the directory nobody's,
    so its ``cover.jpg`` goes to none of them. A library root never belongs
    to an album: everything else lives under it.
    """
    roots = {root.rstrip("/") for root in library_roots}
    folder_albums: Dict[str, Set[str]] = defaultdict(set)
    dir_albums: Dict[str, Set[str]] = defaultdict(set)
    album_dirs: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for album_id, path in tracks:
        folder = album_folder_for_path(path)
        directory = os.path.dirname(path)
        folder_albums[folder].add(album_id)
        dir_albums[directory].add(album_id)
        album_dirs[album_id].add((folder, directory))

    result: Dict[str, AlbumFolders] = {}
    for album_id, pairs in album_dirs.items():
        folders = sorted(
            {
                folder
                for folder, _ in pairs
                if folder_albums[folder] == {album_id} and folder not in roots
            },
            key=natural_key,
        )
        discs = sorted(
            {
                directory
                for folder, directory in pairs
                if directory != folder
                and dir_albums[directory] == {album_id}
                and directory not in roots
            },
            key=natural_key,
        )
        result[album_id] = AlbumFolders(folders, discs)
    return result


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


#: A blank or single-colour picture hashes to this, whatever its colour.
_FLAT = "0" * 16


def track_variants(
    members: Iterable[Member],
    cover_dhash: Optional[str],
    cover_source: Optional[str],
    singles: bool = False,
) -> List[List[Member]]:
    """The tracks whose own embedded cover differs from their album's, one
    list per picture, each in track order.

    The rest show the album's cover and need nothing stored, as do all of an
    album whose cover is unknown. A folder cover is a scan, often bordered or
    cropped unlike the thumbnail the files embed, so only a clearly different
    picture counts against it. A flat picture says too little to count.

    @param singles The members are loose tracks with no album cover of their
        own to fall back on: every one with a picture keeps it.
    """
    ordered = sorted(members, key=track_order)
    if singles:
        return [[member] for member in ordered if member.phash]

    if not cover_dhash:
        return []

    def differs(phash: str) -> bool:
        distance = hamming(phash, cover_dhash)
        if cover_source == FOLDER_ART:
            return distance >= ART_DIFF_MIN_HAMMING
        return distance > ART_SAME_MAX_HAMMING

    groups: List[List[Member]] = []
    for member in ordered:
        if member.phash and member.phash != _FLAT and differs(member.phash):
            _join(groups, member)
    return groups
