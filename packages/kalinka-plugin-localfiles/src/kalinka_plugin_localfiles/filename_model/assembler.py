"""Turn the model's labelled spans into the fields this library stores.

Pure: no model, no config, no I/O. Every rule here exists because the CRF and
this library disagree about something specific, and each is pinned by a test
named for the case it protects.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..utils.name_utils import album_folder_for_path, normalize_for_id
from ..utils.tracklist_names import name_carries_title
from ._vendor.filename_parser.common import SIDE_LETTERS, numeric_value

# A span below its floor is discarded. Swept against the embedded tags of a
# 526-track library, which the parser never saw: artist and album accuracy are
# flat from 0.0 up to these values and fall above them, so each floor is the
# highest one that costs nothing — it mints fewer rows for the same number of
# right answers (album alone: 26 fewer albums, same 45.4%). Artist stops at
# 0.50 rather than 0.70 because the two score the same on ordinary paths while
# 0.50 is 7 points better on bulk downloads. Title has no floor: a rejected
# title falls back to the stem, which is the placeholder it was meant to
# replace. Disc number borrows the track-number floor: the side-lettered rows
# that exercise it score above 0.99, so no sweep separates the two.
MIN_SCORE = {
    "artist": 0.50,
    "album": 0.70,
    "title": 0.0,
    "track_number": 0.50,
    "disc_number": 0.50,
    "year": 0.50,
}

# Reparse path-only rows when assembly rules change, as with new weights.
ASSEMBLER_VERSION = 2

# "058-166421-Artist-Title.mp3": a playlist position and a download id, not a
# track number. The model was never trained on these — they fail label
# alignment upstream — and reads the id as the track number and the containing
# folder as the artist. Stripping both groups and dropping the directory
# context takes this library's 69 such files from 0/69 correct artists on the
# old heuristic to 44/69.
BULK_DOWNLOAD_PREFIX = re.compile(r"^(\d{2,3})-(\d{5,})-")

# "B1 Goodbye Blue Sky.flac" — a vinyl side and a position on it, in the one
# place no tag reaches. Deliberately only a question: it decides whether the
# path is worth parsing, never what the answer is, so a name that merely looks
# like a marker ("D12 - My Band.mp3") costs one parse and no wrong number.
_SIDE_MARKER = re.compile(rf"^[{SIDE_LETTERS}]\d{{1,2}}[ ._-]", re.IGNORECASE)

_UNSPACED_DASH = re.compile(r"[-–—]")
_CLOSERS = {")": "(", "]": "[", "}": "{"}


@dataclass(frozen=True)
class PathMetadata:
    """What a path says about a track, before any name repair."""

    title: str
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    year: Optional[int] = None
    album: Optional[str] = None
    artist: Optional[str] = None

    def as_metadata_dict(self, repair: Callable[[str], str]) -> Dict[str, object]:
        """The shape the enricher consumes, with ``repair`` applied to names.

        Mirrors the old heuristic's contract: ``title`` and ``track_number``
        are always present, the rest only when the path said something.
        """
        metadata: Dict[str, object] = {
            "title": repair(self.title),
            "track_number": self.track_number,
        }
        for key, value in (
            ("album", self.album),
            ("artist", self.artist),
            ("disc_number", self.disc_number),
            ("year", self.year),
        ):
            if value is not None:
                metadata[key] = repair(value) if isinstance(value, str) else value
        return metadata


def names_a_vinyl_side(file_path: str) -> bool:
    """Whether the basename opens on a side marker, e.g. ``B1 …``.

    A side is written into the file's own name rather than a ``CD2`` folder,
    so it is the one disc number a fully tagged rip can still be missing.
    Matched on the stem: the marker has to be followed by a separator and the
    title it numbers, which the extension's own dot must not stand in for.
    """
    return bool(_SIDE_MARKER.match(_stem(file_path)))


def build_view(file_path: str, music_root: Optional[str]) -> Optional[str]:
    """The substring of ``file_path`` the model should see, or None.

    Keeps the album folder, whatever names it, and one level above it, which
    is where an artist folder sits. ``context_window`` upstream trims blindly
    by component count and reads the topmost directory of anything deeper as
    the artist. ``album_folder_for_path`` already steps over a ``CD1`` /
    ``Disc 2`` subdir, so a disc folder never eats the artist's slot.
    """
    if not file_path or not music_root:
        return None
    basename = os.path.basename(file_path)
    if BULK_DOWNLOAD_PREFIX.match(os.path.splitext(basename)[0]):
        stem, extension = os.path.splitext(basename)
        return BULK_DOWNLOAD_PREFIX.sub("", stem) + extension
    album_parent = os.path.dirname(album_folder_for_path(file_path))
    window_root = os.path.dirname(album_parent)
    if len(window_root) < len(music_root):
        window_root = music_root
    try:
        view = os.path.relpath(file_path, window_root)
    except ValueError:
        return None
    return None if view.startswith(os.pardir) else view


def assemble(
    view: str,
    result: Dict,
    *,
    is_placeholder_artist: Callable[[str], bool],
    floors: Dict[str, float] = MIN_SCORE,
) -> PathMetadata:
    """Fields from one parse of ``view``.

    ``is_placeholder_artist`` decides whether a folder naming an artist really
    names one; a various-artists folder does not, and its per-file
    "Artist - Title" is then the only place a track's artist appears.
    """
    spans = [s for s in result.get("spans", ()) if _clears_floor(s, floors)]
    components = _components(view)
    basename_start = components[-1][0] if components else 0

    artist = _choose_artist(spans, basename_start, is_placeholder_artist)
    album, artist = _resolve_album(spans, components, artist)
    title = _build_title(view, spans, components, basename_start)

    if artist is not None and artist["start"] >= basename_start:
        absorbed = _absorb_into_title(view, artist, title)
        if absorbed is not None:
            title, artist = absorbed, None
    elif artist is not None:
        basename_artist = _last(_labelled(spans, "ARTIST", at_or_after=basename_start))
        if basename_artist is not None and not _same_name(basename_artist, artist):
            title = _extend_left(title, basename_artist)

    # A ripper's numbered placeholder names no artist. Keep an artist read
    # from a directory, but never turn the "track" in track-02 into one.
    if (
        artist is not None
        and artist["start"] >= basename_start
        and not name_carries_title(view, [view])
    ):
        artist = None

    return PathMetadata(
        title=_slice(view, title) or _stem(view),
        artist=_text(view, artist),
        album=_text(view, album),
        track_number=_number(
            _last(_labelled(spans, "TRACK_NUMBER", at_or_after=basename_start))
        ),
        disc_number=_number(_last(_labelled(spans, "DISC_NUMBER"))),
        # The parser's own rule: a path carrying two years states the original
        # release first and the reissue second.
        year=_number(_first(_labelled(spans, "YEAR"))),
    )


def _clears_floor(span: Dict, floors: Dict[str, float]) -> bool:
    return span.get("score", 1.0) >= floors.get(span["label"].lower(), 0.0)


def _components(view: str) -> List[Tuple[int, int]]:
    return [m.span() for m in re.finditer(r"[^/\\]+", view)]


def _labelled(
    spans: Sequence[Dict], label: str, *, at_or_after: Optional[int] = None
) -> List[Dict]:
    return [
        s
        for s in spans
        if s["label"] == label and (at_or_after is None or s["start"] >= at_or_after)
    ]


def _first(spans: Sequence[Dict]) -> Optional[Dict]:
    return min(spans, key=lambda s: s["start"], default=None)


def _last(spans: Sequence[Dict]) -> Optional[Dict]:
    return max(spans, key=lambda s: s["start"], default=None)


def _same_name(left: Dict, right: Dict) -> bool:
    return normalize_for_id(left["text"]) == normalize_for_id(right["text"])


def _choose_artist(
    spans: Sequence[Dict],
    basename_start: int,
    is_placeholder_artist: Callable[[str], bool],
) -> Optional[Dict]:
    """A folder that names its artist speaks for every file inside it.

    Structural rather than score-based: the deepest directory that names an
    artist wins, and only when nothing above it does does the basename get a
    say. A various-artists folder names nobody, so it is skipped.
    """
    candidates = _labelled(spans, "ARTIST")
    directory = [
        s
        for s in candidates
        if s["start"] < basename_start and not is_placeholder_artist(s["text"])
    ]
    return _last(directory) or _last(
        [s for s in candidates if s["start"] >= basename_start]
    )


def _resolve_album(
    spans: Sequence[Dict],
    components: Sequence[Tuple[int, int]],
    artist: Optional[Dict],
) -> Tuple[Optional[Dict], Optional[Dict]]:
    """The album span, never widened over an edition or a technical tag.

    A lone parent folder reads as an artist to the model, while this library
    treats one folder as one album, so an artist filling the album folder
    exactly is read as the album instead.
    """
    album = _last(_labelled(spans, "ALBUM"))
    if album is not None or artist is None or len(components) < 2:
        return album, artist
    if (artist["start"], artist["end"]) == components[-2]:
        return artist, None
    return None, artist


def _build_title(
    view: str,
    spans: Sequence[Dict],
    components: Sequence[Tuple[int, int]],
    basename_start: int,
) -> Optional[Dict]:
    titles = _labelled(spans, "TITLE", at_or_after=basename_start) or _labelled(
        spans, "TITLE"
    )
    if not titles:
        return None
    title = {
        "start": min(s["start"] for s in titles),
        "end": max(s["end"] for s in titles),
    }
    return _extend_over_edition(view, spans, components, title, basename_start)


def _extend_over_edition(
    view: str,
    spans: Sequence[Dict],
    components: Sequence[Tuple[int, int]],
    title: Dict,
    basename_start: int,
) -> Dict:
    """Take a version qualifier back into the title it qualifies.

    The tags in a real library read "Us And Them (2011 Remastered Version)",
    and an edition in a *directory* qualifies the album rather than this
    track, so only the basename's own editions count. The brackets around an
    edition carry no label, so the span stops short of the closing one.
    """
    editions = [
        s
        for s in _labelled(spans, "EDITION", at_or_after=basename_start)
        if s["start"] >= title["end"]
    ]
    if not editions:
        return title
    end = max(s["end"] for s in editions)
    component_end = components[-1][1] if components else len(view)
    while end < component_end and view[end] in _CLOSERS:
        opener = _CLOSERS[view[end]]
        enclosed = view[title["start"] : end]
        if enclosed.count(opener) <= enclosed.count(view[end]):
            break
        end += 1
    return {**title, "end": end}


def _absorb_into_title(
    view: str, artist: Dict, title: Optional[Dict]
) -> Optional[Dict]:
    """An unspaced dash before a lowercase word is a hyphen, not a separator.

    "Красно-желтые дни" is one title; "Кино-Пачка сигарет" is an artist and a
    title. The model splits both the same way, so the case of the following
    word is the only signal that separates them.
    """
    if title is None or artist["end"] > title["start"]:
        return None
    if not _UNSPACED_DASH.fullmatch(view[artist["end"] : title["start"]]):
        return None
    head = view[title["start"] : title["end"]][:1]
    return _extend_left(title, artist) if head.islower() else None


def _extend_left(title: Optional[Dict], span: Dict) -> Optional[Dict]:
    if title is None or span["end"] > title["start"]:
        return title
    return {**title, "start": span["start"]}


def _slice(view: str, span: Optional[Dict]) -> str:
    return view[span["start"] : span["end"]] if span else ""


def _text(view: str, span: Optional[Dict]) -> Optional[str]:
    return _slice(view, span) or None


def _number(span: Optional[Dict]) -> Optional[int]:
    # A vinyl side arrives as the letter it is written with, so the reading of
    # a numeric span belongs to the parser that labelled it, not to int().
    return numeric_value(span["label"], span["text"]) if span else None


def _stem(view: str) -> str:
    return os.path.splitext(os.path.basename(view))[0]
