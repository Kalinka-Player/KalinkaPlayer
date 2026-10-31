"""Pick an album's cover from the image files sitting beside its audio.

A rip often ships its sleeve as ordinary files in the album folder — the scan
its owner made, or the cover a downloader saved — and for a vinyl needledrop
that is the most authoritative art available: a photograph of the object that
was actually played, needing no network and no match.

A conventional name (``cover.jpg``, ``folder.jpg``) settles it. Otherwise
names rarely say which is the front (``beatles_abbey_1.jpg`` beside
``beatles_abbey_d1.jpg``), so the choice is made from what the files *are*,
with the name only breaking ties.
"""

from __future__ import annotations

import os
import re
from typing import Callable, List, NamedTuple, Optional, Tuple

from PIL import Image

from ..housekeeping import is_hidden_file, is_housekeeping_dir
from ..storage import DirEntry, FileStorage

#: A cheap pre-filter on the *name*, so most files are ruled out without
#: being opened; the real format is whatever the header turns out to say,
#: and rips do misname things (a "pic.gif" holding a JPEG). Archival
#: extensions are left out deliberately: TIFF sits beside a rip only rarely
#: and one sleeve scan can run to hundreds of megabytes.
_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

#: Wider than what the indexer reads: a folder of music is another album's
#: folder whether or not this library can play the format.
_AUDIO_EXTENSIONS = (
    ".flac", ".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".ape", ".wv",
    ".ogg", ".oga", ".opus", ".wma", ".dsf", ".dff", ".mpc", ".alac",
)

#: The memory guard, applied to the directory entry before the file is
#: opened at all. Real cover art does not come close; archival scans do.
_MAX_BYTES = 32 * 1024 * 1024
#: Pillow refuses to decode beyond this, so anything larger is declined here
#: rather than raised over later. A 600 dpi scan of a 12" sleeve is ~53 Mpx
#: and stays comfortably inside it.
_MAX_PIXELS = Image.MAX_IMAGE_PIXELS or 89_478_485

#: The names players conventionally look for, best first. A file named so
#: is the album's cover by its owner's say-so, whatever it looks like.
_EXACT_STEMS = ("cover", "folder", "front", "album")

_PREFERRED_RE = re.compile(r"(cover|front|folder|albumart|album[ _-]?art|sleeve)", re.I)
_REJECTED_RE = re.compile(
    r"(back|rear|inlay|inside|booklet|disc|cd\d|label|obi|tray|spine|matrix|thumb)",
    re.I,
)
# Names that say a picture is something other than a front, from where a
# word starts ("BackCover"), unlike "disc": "CD1 Cover.jpg" is still a cover.
_NOT_THE_FRONT_RE = re.compile(
    r"(?<![a-z])(?:back|rear|inlay|inside|booklet|label|obi|tray|spine|matrix"
    r"|thumb)|(?<![a-z])(?:artist|logo)(?![a-z])",
    re.I,
)

#: A named image smaller than this is a player's thumbnail
#: (AlbumArtSmall.jpg), not the sleeve; a conventional name is taken as is.
_MIN_NAMED_SIDE = 300
_ORDINAL_RE = re.compile(r"(\d+)\s*$")

#: A sleeve and a disc label scanned at one resolution differ by their real
#: sizes — a 31 cm sleeve against a 10 cm label is about three to one — and
#: both are square, so nothing else separates them. Half the largest is a
#: generous floor that keeps the sleeve and drops the label.
_MIN_RELATIVE_SIDE = 0.5

#: A CD inlay scanned unfolded in one pass: back panel left, front right.
#: Never exactly two squares, because the back tray is shorter than the front
#: is wide. Measured on real rips: 1.57 to 1.86, against 1.26 for the widest
#: single panel.
_FOLD_MIN_ASPECT, _FOLD_MAX_ASPECT = 1.5, 2.4

#: The right half of such a scan, as fractions of the whole.
_FRONT_PANEL = (0.5, 0.0, 1.0, 1.0)

#: Covers are square give or take a scan border. This only has to exclude
#: shapes that cannot be one: a spine, a panorama, an open booklet spread.
_MIN_ASPECT, _MAX_ASPECT = 0.5, _FOLD_MAX_ASPECT


#: How a folder image came to be a candidate, most trustworthy first: a
#: conventional name (``cover.jpg``), a name that says cover in some other
#: way (``AlbumArt_Large.jpg``), or what the image looks like.
EXACT, NAMED, SCAN = "exact", "named", "scan"


class FolderCover(NamedTuple):
    """Where an album's front cover is: which file, and which part of it.

    @param path The chosen image file.
    @param box  The front panel as ``(left, top, right, bottom)`` fractions
        of the image, or None to use the whole of it. Fractions rather than
        pixels because the reader may decode at a reduced scale.
    @param tier :data:`EXACT`, :data:`NAMED` or :data:`SCAN`.
    """

    path: str
    box: Optional[Tuple[float, float, float, float]]
    tier: str = SCAN


class FolderCovers(NamedTuple):
    """What a search of an album folder turned up.

    @param covers The images that could be the front cover, most likely
        first; a caller whose first choice will not decode takes the next.
    @param directories Every directory searched. Adding an image to one
        changes its mtime, so these say whether the answer could differ.
    @param images Every image file found, whether or not it could be the
        cover: one still being copied is passed over, and finishing the copy
        changes the file but not its directory.
    """

    covers: List[FolderCover]
    directories: List[str]
    images: List[str]


class _Candidate(NamedTuple):
    path: str
    width: int
    height: int

    @property
    def longest_side(self) -> int:
        return max(self.width, self.height)

    @property
    def ordinal(self) -> int:
        """The number the name ends with, or 0. Scan sets run front-first."""
        match = _ORDINAL_RE.search(_stem(self.path))
        return int(match.group(1)) if match else 0

    @property
    def is_folded(self) -> bool:
        """Whether this looks like an unfolded two-panel inlay."""
        return _FOLD_MIN_ASPECT <= self.width / self.height <= _FOLD_MAX_ASPECT

    def as_cover(self, tier: str) -> "FolderCover":
        return FolderCover(
            self.path, _FRONT_PANEL if self.is_folded else None, tier
        )


def is_cover_image_name(name: str) -> bool:
    """Whether a file could be one of a folder's cover images, by its name."""
    return not is_hidden_file(name) and name.lower().endswith(_EXTENSIONS)


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _exact_rank(path: str) -> Optional[int]:
    """Where a conventional name stands in :data:`_EXACT_STEMS`, or None."""
    stem = _stem(path).lower()
    return _EXACT_STEMS.index(stem) if stem in _EXACT_STEMS else None


def _holds_audio(storage: FileStorage, directory: str) -> bool:
    """Whether a directory has music of its own directly in it."""
    try:
        return any(
            entry.name.lower().endswith(_AUDIO_EXTENSIONS)
            for entry in storage.listdir(directory)
        )
    except OSError:
        return False


def _directories(
    storage: FileStorage,
    folder: str,
    holds_tracks: Optional[Callable[[str], bool]] = None,
    strict: bool = False,
) -> List[str]:
    """``folder`` and the subdirectories that hold artwork rather than music.

    Scans are often filed under PIC/, Artwork/ or Scans/ rather than beside
    the audio, so one level down is searched too — and no further, to keep a
    deep tree of unrelated images out. A subdirectory with music of its own
    is another album's folder rather than this one's scans: a loose track
    sitting among album folders would otherwise be handed a neighbour's
    sleeve. ``holds_tracks`` says the same of a subdirectory the library
    indexed tracks under at any depth, which a listing one level down cannot.
    """
    try:
        children = sorted(storage.listdir(folder), key=lambda e: e.name)
    except OSError:
        if strict:
            raise
        return []
    return [folder] + [
        entry.path
        for entry in children
        if _searchable_directory(storage, entry)
        and not (holds_tracks and holds_tracks(entry.path))
        and not _holds_audio(storage, entry.path)
    ]


def album_directories(
    storage: FileStorage,
    folders: List[str],
    discs: List[str],
    holds_tracks: Callable[[str], bool],
) -> List[str]:
    """Where an album's cover may be, best first: each of its folders and
    the artwork folders under it, then its disc folders.

    @raise OSError If one of its folders cannot be listed, which says
        nothing about what it holds.
    """
    directories: List[str] = []
    for folder in folders:
        directories.extend(_directories(storage, folder, holds_tracks, strict=True))
    directories.extend(disc for disc in discs if disc not in directories)
    return directories


def _searchable_directory(storage: FileStorage, entry: DirEntry) -> bool:
    """Whether ``entry`` is a directory worth searching for artwork.

    A listing reports a link to a directory as a file, because a scan must
    not descend through one. Artwork is the other question: a ``Scans``
    folder kept as a link is ordinary, and this looks exactly one level
    down rather than walking, so following it cannot loop. The extra stat
    that costs is spent only on entries that could be one — anything
    carrying a file extension is a file. A NAS's thumbnail cache or bin is
    somebody's housekeeping, whatever images it holds.
    """
    if is_housekeeping_dir(entry.name):
        return False
    if entry.is_dir:
        return True
    if os.path.splitext(entry.name)[1]:
        return False
    return storage.is_dir(entry.path)


def _image_entries(
    storage: FileStorage, directories: List[str], strict: bool = False
) -> List[DirEntry]:
    """Image files, by name alone — no file is opened here."""
    entries: List[DirEntry] = []
    for directory in directories:
        try:
            children = sorted(storage.listdir(directory), key=lambda e: e.name)
        except OSError:
            if strict:
                raise
            continue
        entries.extend(
            entry
            for entry in children
            if not entry.is_dir and is_cover_image_name(entry.name)
        )
    return entries


def _worth_measuring(storage: FileStorage, entry: DirEntry) -> bool:
    size = storage.size_of(entry)
    # Zero is an empty file or one the storage could not measure;
    # neither is worth a round trip to open.
    return bool(size) and size <= _MAX_BYTES


def _measure(storage: FileStorage, path: str) -> Optional[_Candidate]:
    """Dimensions read from the header, or None when the file is unusable."""
    try:
        with storage.open(path) as handle, Image.open(handle) as image:
            width, height = image.size
    except Exception:
        return None
    if not width or not height or width * height > _MAX_PIXELS:
        return None
    return _Candidate(path, width, height)


def find_folder_covers(
    storage: FileStorage,
    folder: str,
    directories: Optional[List[str]] = None,
) -> FolderCovers:
    """The images in ``folder`` that could be its front cover, best first.

    @param storage Where the folder lives; the same call serves a local
        album folder and one on a share.
    @param folder The album's directory; unless ``directories`` says
        otherwise, its immediate subdirectories without music are searched
        too.
    @param directories The directories to search, in the order their images
        are preferred, for a caller that knows better which belong to the
        album. One of those that cannot be listed raises OSError.
    @return Which files could hold the cover and which part of each is the
        front, with the directories and images that were searched for them.
        Covers come by tier — every :data:`EXACT` one, then :data:`NAMED`,
        then :data:`SCAN` — and within a tier by directory order.
    """
    if not folder:
        return FolderCovers([], [], [])
    strict = directories is not None
    if directories is None:
        directories = _directories(storage, folder)
    images = _image_entries(storage, directories, strict)
    measured = (
        _measure(storage, e.path) for e in images if _worth_measuring(storage, e)
    )
    candidates = [c for c in measured if c is not None]
    return FolderCovers(
        _ranked(candidates, directories),
        list(directories),
        [e.path for e in images],
    )


def find_folder_cover(
    storage: FileStorage, folder: str
) -> Optional[FolderCover]:
    """The first of :func:`find_folder_covers`, or None when the folder
    offers nothing that could be a cover."""
    covers = find_folder_covers(storage, folder).covers
    return covers[0] if covers else None


def _ranked(
    candidates: List[_Candidate], directories: List[str]
) -> List[FolderCover]:
    """The candidates that could be the front cover, most likely first."""
    order = {d.rstrip("/"): index for index, d in enumerate(directories)}

    def place(c: _Candidate) -> int:
        return order.get(os.path.dirname(c.path).rstrip("/"), len(order))

    exact = sorted(
        (c for c in candidates if _exact_rank(c.path) is not None),
        key=lambda c: (place(c), _exact_rank(c.path), c.path),
    )
    # A scan that is already the front beats one it must be cut out of.
    named = sorted(
        (
            c
            for c in candidates
            if _exact_rank(c.path) is None
            and _PREFERRED_RE.search(_stem(c.path))
            and not _NOT_THE_FRONT_RE.search(_stem(c.path))
            and c.longest_side >= _MIN_NAMED_SIDE
        ),
        key=lambda c: (place(c), c.is_folded, -c.longest_side, c.path),
    )
    others = [
        c
        for c in candidates
        if not _PREFERRED_RE.search(_stem(c.path))
        and _exact_rank(c.path) is None
        and not _REJECTED_RE.search(_stem(c.path))
        and not _NOT_THE_FRONT_RE.search(_stem(c.path))
        and _MIN_ASPECT <= c.width / c.height <= _MAX_ASPECT
    ]
    if not exact and not named and not others:
        return []

    # The named images count toward the floor, so that a disc label is not
    # promoted to the front when the sleeve named cover.jpg will not decode.
    floor = max(c.longest_side for c in exact + named + others) * _MIN_RELATIVE_SIDE
    sleeves = sorted(
        (c for c in others if c.longest_side >= floor),
        key=lambda c: (c.ordinal, -c.longest_side, c.path),
    )
    return (
        [c.as_cover(EXACT) for c in exact]
        + [c.as_cover(NAMED) for c in named]
        + [c.as_cover(SCAN) for c in sleeves]
    )
