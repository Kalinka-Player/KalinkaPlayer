"""Pick an album's cover from the image files sitting beside its audio.

A rip often ships its sleeve as ordinary files in the album folder — the scan
its owner made, or the cover a downloader saved — and for a vinyl needledrop
that is the most authoritative art available: a photograph of the object that
was actually played, needing no network and no match.

Their names rarely say which is the front (``beatles_abbey_1.jpg`` beside
``beatles_abbey_d1.jpg``), so the choice is made from what the files *are*,
with the name only breaking ties.
"""

from __future__ import annotations

import os
import re
from typing import List, NamedTuple, Optional, Tuple

from PIL import Image

from ..storage import DirEntry, FileStorage

#: A cheap pre-filter on the *name*, so most files are ruled out without
#: being opened; the real format is whatever the header turns out to say,
#: and rips do misname things (a "pic.gif" holding a JPEG). Archival
#: extensions are left out deliberately: TIFF sits beside a rip only rarely
#: and one sleeve scan can run to hundreds of megabytes.
_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif")

#: Wider than what the indexer reads: a folder of music is another album's
#: folder whether or not this library can play the format.
_AUDIO_EXTENSIONS = (
    ".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ape", ".wv",
    ".ogg", ".opus", ".wma", ".dsf", ".dff", ".mpc", ".alac",
)

#: The memory guard, applied to the directory entry before the file is
#: opened at all. Real cover art does not come close; archival scans do.
_MAX_BYTES = 32 * 1024 * 1024
#: Pillow refuses to decode beyond this, so anything larger is declined here
#: rather than raised over later. A 600 dpi scan of a 12" sleeve is ~53 Mpx
#: and stays comfortably inside it.
_MAX_PIXELS = Image.MAX_IMAGE_PIXELS or 89_478_485

_PREFERRED_RE = re.compile(r"(cover|front|folder|albumart|album[ _-]?art|sleeve)", re.I)
_REJECTED_RE = re.compile(
    r"(back|rear|inlay|inside|booklet|disc|cd\d|label|obi|tray|spine|matrix|thumb)",
    re.I,
)
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


class FolderCover(NamedTuple):
    """Where an album's front cover is: which file, and which part of it.

    @param path The chosen image file.
    @param box  The front panel as ``(left, top, right, bottom)`` fractions
        of the image, or None to use the whole of it. Fractions rather than
        pixels because the reader may decode at a reduced scale.
    """

    path: str
    box: Optional[Tuple[float, float, float, float]]


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

    def as_cover(self) -> "FolderCover":
        return FolderCover(self.path, _FRONT_PANEL if self.is_folded else None)


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _holds_audio(storage: FileStorage, directory: str) -> bool:
    """Whether a directory has music of its own directly in it."""
    try:
        return any(
            entry.name.lower().endswith(_AUDIO_EXTENSIONS)
            for entry in storage.listdir(directory)
        )
    except OSError:
        return False


def _directories(storage: FileStorage, folder: str) -> List[str]:
    """``folder`` and the subdirectories that hold artwork rather than music.

    Scans are often filed under PIC/, Artwork/ or Scans/ rather than beside
    the audio, so one level down is searched too — and no further, to keep a
    deep tree of unrelated images out. A subdirectory with music of its own
    is another album's folder rather than this one's scans: a loose track
    sitting among album folders would otherwise be handed a neighbour's
    sleeve.
    """
    try:
        children = sorted(storage.listdir(folder), key=lambda e: e.name)
    except OSError:
        return []
    return [folder] + [
        entry.path
        for entry in children
        if _searchable_directory(storage, entry)
        and not _holds_audio(storage, entry.path)
    ]


def _searchable_directory(storage: FileStorage, entry: DirEntry) -> bool:
    """Whether ``entry`` is a directory worth searching for artwork.

    A listing reports a link to a directory as a file, because a scan must
    not descend through one. Artwork is the other question: a ``Scans``
    folder kept as a link is ordinary, and this looks exactly one level
    down rather than walking, so following it cannot loop. The extra stat
    that costs is spent only on entries that could be one — anything
    carrying a file extension is a file.
    """
    if entry.is_dir:
        return True
    if os.path.splitext(entry.name)[1]:
        return False
    return storage.is_dir(entry.path)


def _image_entries(storage: FileStorage, folder: str) -> List[DirEntry]:
    """Admissible image files, cheaply filtered — no file is opened here."""
    entries: List[DirEntry] = []
    for directory in _directories(storage, folder):
        try:
            children = sorted(storage.listdir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in children:
            if entry.is_dir or not entry.name.lower().endswith(_EXTENSIONS):
                continue
            size = storage.size_of(entry)
            # Zero is an empty file or one the storage could not measure;
            # neither is worth a round trip to open.
            if not size or size > _MAX_BYTES:
                continue
            entries.append(entry)
    return entries


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


def find_folder_cover(
    storage: FileStorage, folder: str
) -> Optional[FolderCover]:
    """The image in ``folder`` most likely to be its front cover, or None.

    @param storage Where the folder lives; the same call serves a local
        album folder and one on a share.
    @param folder The album's directory; its immediate subdirectories are
        searched too.
    @return Which file holds the cover and which part of it is the front,
        or None when the folder offers nothing that could be a cover.
    """
    if not folder:
        return None
    measured = (_measure(storage, e.path) for e in _image_entries(storage, folder))
    candidates = [c for c in measured if c is not None]

    # A name that says "cover" settles it, whatever the shape — and a scan
    # that is already the front beats one the front must be cut out of.
    named = [c for c in candidates if _PREFERRED_RE.search(_stem(c.path))]
    if named:
        return max(
            named, key=lambda c: (not c.is_folded, c.longest_side, c.path)
        ).as_cover()

    candidates = [
        c
        for c in candidates
        if not _REJECTED_RE.search(_stem(c.path))
        and _MIN_ASPECT <= c.width / c.height <= _MAX_ASPECT
    ]
    if not candidates:
        return None

    floor = max(c.longest_side for c in candidates) * _MIN_RELATIVE_SIDE
    sleeves = [c for c in candidates if c.longest_side >= floor]
    return min(sleeves, key=lambda c: (c.ordinal, -c.longest_side, c.path)).as_cover()
