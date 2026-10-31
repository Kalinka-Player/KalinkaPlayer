"""Shared V/A-compilation and disc-suffix helpers.

Single source of truth for the folder-classification logic that used to live
only inside the indexer's orphan_va pass. Both the indexer and the clustering
planner import from here so the two cannot drift.
"""

import os
import re
from typing import Optional

# A folder's tracks coalesce into a single V/A compilation album when they
# span at least this many distinct artists AND at least this fraction of
# tracks have unique artists. Both, so a mistagged album (Abbey Road with a
# couple of wrong-artist tags) doesn't flip to V/A.
VA_MIN_DISTINCT_ARTISTS = 4
VA_MIN_ARTIST_UNIQUENESS = 0.5

VARIOUS_ARTISTS_ID = "various_artists"

# Structural names supply no album identity, even for a single artist.
_CONTAINER_NAMES = (
    r"music|musik|audio|downloads?|mp3s?|tracks?|songs?|various|"
    r"streamed_music|unused|unsorted|sorted|misc|miscellaneous|temp|tmp|"
    r"incoming|untitled|new folder|to ?sort|todo"
)
CONTAINER_FOLDER_RE = re.compile(rf"^({_CONTAINER_NAMES})$", re.IGNORECASE)
# A many-artist mix can also be a loose playlist, but a single-artist album
# such as "The Mix" must not be discarded merely because of its name.
GENERIC_FOLDER_RE = re.compile(
    rf"^({_CONTAINER_NAMES}|.*\bmix(?:es)?\b.*)$",
    re.IGNORECASE,
)
# Bare disc/volume folder names that need the parent dir for a real title.
BARE_DISC_RE = re.compile(
    r"^(cd[-_ ]?\d+|disc\s*\d+|disk\s*\d+|volume\s*\d+|vol\.?\s*\d+)$",
    re.IGNORECASE,
)
# The names a compilation uses instead of an artist, in one place: as a
# folder prefix ("VA - Best of 90s") and standing alone (the artist half of
# "Various Artists - Best of 90s").
# The trailing dot is optional: a span carved out of "V.A. - Hits 1995"
# arrives as "V.A", the dot having been trimmed as a separator edge.
_VA_NAME = r"va|v\.\s*a\.?|various(?:\s+artists?)?"
VA_PREFIX_RE = re.compile(rf"^(?:{_VA_NAME})\s*[-–—]\s*", re.IGNORECASE)
_VA_NAME_RE = re.compile(rf"^(?:{_VA_NAME})$", re.IGNORECASE)


def is_various_artists_name(name: str) -> bool:
    """True when a name is the various-artists placeholder rather than a real
    artist, so per-file "Artist - Title" naming still gets a say."""
    return bool(name and _VA_NAME_RE.match(name.strip()))
# Canonical I–XCIX avoids treating words such as "CD Mix" as disc numbers.
_ROMAN_DISC_NUMBER = r"(?=[ivxl])(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
# A trailing "(Disc 2)" / "CD I" / "Vol. 3" on an album title.
_DISC_SUFFIX_RE = re.compile(
    rf"[\s\-–—_([]+(?:cd|disc|disk|volume|vol\.?)\s*"
    rf"(?:\d+|{_ROMAN_DISC_NUMBER})\s*[)\]]?\s*$",
    re.IGNORECASE,
)


def is_va_folder(distinct_artists: int, n_tracks: int) -> bool:
    """Whether a folder qualifies as various-artists by the count thresholds."""
    if n_tracks <= 0:
        return False
    return (
        distinct_artists >= VA_MIN_DISTINCT_ARTISTS
        and (distinct_artists / n_tracks) >= VA_MIN_ARTIST_UNIQUENESS
    )


def is_declared_va_folder(folder: str) -> bool:
    """True when the folder name explicitly declares a various-artists
    compilation ("VA - X" / "Various Artists - X"). Such a folder is a
    deliberate compilation even without shared album tags, so the flat-dump
    detach heuristic must leave it alone."""
    name = os.path.basename(folder.rstrip("/")).strip()
    return bool(VA_PREFIX_RE.match(name))


def compilation_title(folder: str) -> Optional[str]:
    """Album title for a V/A folder, or None if it's a generic dump.

    A folder explicitly marked ``VA -`` / ``Various Artists -`` is a declared
    compilation and bypasses the generic-dump heuristic.
    """
    folder = folder.rstrip("/")  # a trailing "/" would make basename empty
    name = os.path.basename(folder).strip()
    title = VA_PREFIX_RE.sub("", name).strip()
    explicit_va = title != name
    if not title:
        return None
    if not explicit_va and GENERIC_FOLDER_RE.match(title):
        return None
    if BARE_DISC_RE.match(title):
        parent = os.path.basename(os.path.dirname(folder)).strip()
        title = f"{parent} {title}".strip() if parent else title
    return title or None


def strip_artist_prefix(title: str, artist_name: str) -> str:
    """Drop a leading artist name from a title (``Ratatat Remixes`` ->
    ``Remixes``). Only at a separator/word boundary, so ``AB`` does not
    corrupt ``ABBA Gold``."""
    if title.lower().startswith(artist_name.lower()):
        rest = title[len(artist_name):]
        if not rest or rest[0] in " -–—":
            return rest.lstrip(" -–—") or title
    return title


def strip_disc_suffix(title: str) -> str:
    """Remove a trailing disc/volume marker so multi-disc-tagged sets share an
    album key (``Album (Disc 1)`` and ``Album (Disc 2)`` -> ``Album``)."""
    if not title:
        return title
    return _DISC_SUFFIX_RE.sub("", title).strip() or title


# A cue sheet titles one disc, often in words the tag-oriented suffix rule
# misses: "Disc One", "CD-2", "[CD 1 of 2]".
_CUE_DISC_LABEL_RE = re.compile(
    r"[\s\-–—_:,(\[]+(?:cd|disc|disk)"
    rf"(?:[\s\-#.]+(?:\d+|{_ROMAN_DISC_NUMBER}|one|two|three|four|five|six|seven"
    r"|eight|nine|ten)|\d+)(?:\s*(?:of|/)\s*\d+)?\s*[)\]]?\s*$",
    re.IGNORECASE,
)


def strip_disc_label(title: str) -> str:
    """A cue-titled disc's title without its disc label, in the wider forms
    cue sheets use as well as :func:`strip_disc_suffix`'s."""
    return _CUE_DISC_LABEL_RE.sub("", strip_disc_suffix(title)).strip() or title



# --- title normalization (deterministic cleanup of a local title) -----------
# Album titles are always locally derived (MusicBrainz never sets a title), so
# this is a safe cleanup of folder/tag junk, not a competing title source.

_BRACKET_TAG_RE = re.compile(r"^\s*\{[^}]*\}\s*")          # "{uaoa}Foo"
_YEAR_PREFIX_RE = re.compile(r"^\s*(?:19|20)\d{2}\s*[.\-–—]\s*")  # "1993. Foo"
_SOURCE_TAIL_RE = re.compile(
    r"\s*[-–—]{1,3}\s*(?:jamendo|beatport|bandcamp|soundcloud)\b.*$", re.I)
_DIGITS_TAIL_RE = re.compile(r"\s*[-–—]\s*\d{5,}\s*$")     # "… - 500604904"
_FORMAT_TAIL_RE = re.compile(
    r"\s*[-–—\[(]*\s*(?:mp3|flac|wav|web|webm|ogg|aac|hi-?res)\s*[)\]]*\s*$", re.I)
_TRAILING_PAREN_RE = re.compile(r"\s*[([]([^()\[\]]*)[)\]]\s*$")

_CATALOG_DIGITS_RE = re.compile(r"\d{5,}")
_FORMAT_TOKEN_RE = re.compile(
    r"\b(?:mp3|flac|wav|web|webm|ogg|aac|hi-?res|\d{2,3}-\d{2,3}|"
    r"\d{2,3}\s?khz|\d{2,3}\s?bit)\b", re.I)
# Parentheticals that are meaningful and must be kept.
_DESCRIPTIVE_RE = re.compile(
    r"\b(soundtrack|ost|live|remaster(?:ed)?|deluxe|anniversary|edition|"
    r"remix(?:es)?|acoustic|mono|stereo|demo|bonus|expanded|special|single|"
    r"ep|explicit|instrumental|reissue|collector'?s|complete|sessions?|"
    r"unplugged|original|volume|vol\.?|part|disc|disk|cd)\b", re.I)


def _is_catalog_paren(contents: str) -> bool:
    """True for a pressing/catalog parenthetical (strip), False for a
    descriptive one like (Soundtrack) / (Remastered) / a bare year (keep)."""
    c = contents.strip()
    if not c:
        return False
    # A 5+ digit run is a catalogue/pressing number — decisive even when a
    # descriptive word rides along ("[CD 61407]", "[Disc 12345]"), so this is
    # checked before the descriptive bail below (which keeps "(Disc 1)").
    if _CATALOG_DIGITS_RE.search(c):
        return True
    if _DESCRIPTIVE_RE.search(c):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", c):   # a bare year -> keep
        return False
    if _FORMAT_TOKEN_RE.search(c):
        return True
    # "Label CAT123, Country" shape: has a comma and an uppercase label token.
    return "," in c and bool(re.search(r"[A-Z]{2,}", c))


def normalize_album_title(title: str) -> str:
    """Strip folder/tag junk from a title: leading bracket tag + year prefix,
    trailing source/format/catalog tokens and catalog parentheticals. Keeps
    descriptive parentheticals and disc/volume markers. Returns the original
    if cleanup would empty it."""
    if not title:
        return title
    t = _BRACKET_TAG_RE.sub("", title.strip())
    t = _YEAR_PREFIX_RE.sub("", t)
    t = _SOURCE_TAIL_RE.sub("", t)
    for _ in range(3):  # peel a few trailing catalog parentheticals
        m = _TRAILING_PAREN_RE.search(t)
        if not m or not _is_catalog_paren(m.group(1)):
            break
        t = t[: m.start()].rstrip()
    t = _FORMAT_TAIL_RE.sub("", t)
    t = _DIGITS_TAIL_RE.sub("", t)
    t = t.strip(" -–—_.,/\t")
    return t or title
