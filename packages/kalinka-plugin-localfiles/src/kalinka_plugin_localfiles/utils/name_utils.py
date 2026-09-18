"""Shared name & path normalization for artist/album identity.

Three concerns live here:

- ``normalize_for_id``: aggressive normalization used only as input to the
  ID hash. Collapses surface-level differences so "- Roxy Music" and
  "Roxy Music", or "Jean-Michel Jarre" and "Jean Michel Jarre", produce the
  same stable ID. The output is never shown to a user.

- ``clean_display_name``: light cleanup of what gets stored in
  ``artists.name`` / ``albums.title``. Strips leading/trailing punctuation
  and collapses internal whitespace runs, but leaves the rest of the
  user-visible string untouched (hyphens, case, diacritics preserved).

- ``album_folder_for_path``: maps a track's file path to the directory
  that represents its album. The immediate parent is the album folder,
  unless that parent looks like a disc subdir (``CD1`` / ``Disc 2``) in
  which case we walk up one more level. This is what makes the album ID
  folder-bounded: same album in two quality folders produces two
  distinct album IDs, while ``Disc 1`` and ``Disc 2`` of one set merge.

- ``fold_for_provider_match`` / ``name_similarity``: comparison-only
  normalization and scoring, used when asking an external catalogue whether
  it holds the same record. Never stored.

Keeping these in one place avoids drift between the indexer's
``id_generator.py`` and the enricher's copy.
"""

from __future__ import annotations

import html
import os
import re
import unicodedata

import ftfy
from rapidfuzz import fuzz

from typing import Optional

_LEADING_TRAILING_PUNCT_RE = re.compile(r"^[\-_.,/ \t]+|[\-_.,/ \t]+$")
_WEB_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});")
_DOTTED_ABBREV_RE = re.compile(r"(?<=\w)\.(?=[^\W\d_]{2})")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_PUNCT_FOR_ID_RE = re.compile(r"[\-_./]")
_NON_WORD_RE = re.compile(r"[^\w\s]")
_DISC_SUBDIR_RE = re.compile(r"^(cd|disc|disk)[\s\-_]*\d+$", re.IGNORECASE)


def unescape_web_entities(text: str) -> str:
    """One pass of HTML-entity unrolling for tag text mangled by web tools
    ("Simon &amp; Garfunkel" -> "Simon & Garfunkel").

    Only semicolon-terminated entities from the common mangling set are
    converted — full ``html.unescape`` would also rewrite HTML5 legacy forms
    without a semicolon ("&notabene" -> "¬abene").
    """
    if not text or "&" not in text:
        return text
    return _WEB_ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), text)


def space_dotted_abbreviations(name: str) -> str:
    """Insert the missing space after an abbreviating period for search
    queries ("В.Цой" -> "В. Цой", "J.S.Bach" -> "J.S. Bach"). Dotted
    acronyms ("R.E.M.") and ellipsis prefixes ("...And Justice") are left
    alone: the space lands only between a word character and a following
    token of two or more letters.
    """
    if not name or "." not in name:
        return name
    return _DOTTED_ABBREV_RE.sub(". ", name)


def _script_of(ch: str) -> str:
    """Unicode script prefix of a letter ("CYRILLIC", "GREEK", …); unnamed
    characters count as LATIN so they weigh against a repair."""
    name = unicodedata.name(ch, "")
    return name.split(" ")[0] if name else "LATIN"


def _mojibake_bytes(text: str) -> Optional[bytes]:
    """The original byte sequence of a latin-1/cp1252-misread string, or
    None when the text holds genuine non-latin1 unicode (nothing to
    recover)."""
    for encoding in ("latin-1", "cp1252"):
        try:
            return text.encode(encoding)
        except UnicodeEncodeError:
            continue
    return None


def repair_mojibake(text: str, legacy_encoding: Optional[str] = None) -> str:
    """Undo tag text misread in the wrong encoding, conservatively.

    Tier 1, unconditional: ftfy repairs mojibake whose underlying bytes are
    UTF-8 ("BjÃ¶rk" -> "Björk", double-encoded "BjÃƒÂ¶rk" too) — the class
    that is self-evident from byte structure alone.

    Tier 2, only with a configured ``legacy_encoding`` (cp1251, cp1253, …):
    a single-byte codepage read as latin-1 ("ÐÓÊÈ ÂÂÅÐÕ" -> "РУКИ ВВЕРХ").
    ftfy deliberately does not touch this class: the codepage cannot be
    detected from a short name — the same bytes decode "coherently" in
    every full 8-bit codepage — so it must be declared, and the repair
    fires only when the text is dominated by high-range letters and the
    result lands in one non-Latin script. Accented western names
    ("Mötley Crüe") never qualify.
    """
    if not text:
        return text
    fixed = ftfy.fix_encoding(text)
    if fixed != text:
        return fixed
    if not legacy_encoding:
        return text
    raw = _mojibake_bytes(text)
    if raw is None:
        return text
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    high = sum(1 for c in letters if 0xC0 <= ord(c) <= 0xFF)
    if high / len(letters) < 0.6:
        return text
    try:
        candidate = raw.decode(legacy_encoding)
    except (UnicodeDecodeError, LookupError):
        return text
    cand_scripts = [_script_of(c) for c in candidate if c.isalpha()]
    non_latin = set(cand_scripts) - {"LATIN"}
    if not cand_scripts or len(non_latin) != 1:
        return text
    if sum(s in non_latin for s in cand_scripts) / len(cand_scripts) < 0.9:
        return text
    return candidate


def repair_tag_text(text: str, legacy_encoding: Optional[str] = None) -> str:
    """The full tag-repair chain — mojibake, web entities, dotted
    abbreviations — for any name or title headed for the library."""
    return space_dotted_abbreviations(
        unescape_web_entities(repair_mojibake(text, legacy_encoding))
    )


def clean_display_name(name: str) -> str:
    """Sanitize a name before it's stored in the DB.

    Unrolls web-mangled HTML entities, strips leading/trailing ``-_.,/`` and
    whitespace, and collapses internal whitespace runs. Preserves case,
    hyphens, and diacritics so the stored string still matches what the user
    sees in their file tags.
    """
    if not name:
        return ""
    cleaned = unescape_web_entities(name)
    cleaned = _LEADING_TRAILING_PUNCT_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()
    return cleaned


def album_folder_for_path(file_path: str) -> str:
    """Pick the directory that represents this file's album.

    Returns the file's parent dir, except when that parent is a disc
    subdir (``CD1`` / ``Disc 2`` / ``Disk-3``), in which case the
    grandparent is returned so multi-disc rips collapse into one album.

    Used as the folder component of the album ID, so files in
    ``/Music/RAM 24bit`` and ``/Music/RAM 16bit`` end up in different
    albums even when their tag album-title is identical, while
    ``/.../Album/Disc 1`` and ``/.../Album/Disc 2`` end up in the same
    one.
    """
    if not file_path:
        return ""
    parent = os.path.dirname(file_path)
    name = os.path.basename(parent)
    if _DISC_SUBDIR_RE.match(name):
        return os.path.dirname(parent)
    return parent


def fold_diacritics(name: str) -> str:
    """Strip diacritics while preserving case, spacing, and punctuation.

    NFKD-decompose and drop combining marks so "Női Kabát" folds to
    "Noi Kabat". Unlike :func:`normalize_for_id` the surface form is
    otherwise intact; used by the searcher re-rank to compare an unaccented
    query against accented metadata.
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    return "".join(c for c in n if not unicodedata.combining(c))


def normalize_for_id(name: str) -> str:
    """Aggressive normalization used only for ID hashing.

    Steps:
      1. Unicode NFKD, strip combining marks (so "Beyoncé" hashes like "Beyonce")
      2. Lowercase
      3. Replace ``-_./`` with space (so "Jean-Michel Jarre" hashes like "Jean Michel Jarre")
      4. Drop remaining non-word characters
      5. Collapse whitespace runs and strip
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = _PUNCT_FOR_ID_RE.sub(" ", n)
    n = _NON_WORD_RE.sub("", n)
    n = _WHITESPACE_RUN_RE.sub(" ", n).strip()
    return n


# Bracketed qualifiers a catalogue adds to the same record. Matching has to
# see through them; ``normalize_album_title`` deliberately keeps descriptive
# parentheticals, so this is a separate, comparison-only fold.
_EDITION_WORDS = (
    "remaster|remastered|deluxe|expanded|edition|anniversary|reissue|"
    "mono|stereo|version|bonus|remix|explicit|clean"
)
_EDITION_PAREN_RE = re.compile(
    rf"\s*[\(\[\{{][^)\]\}}]*(?:{_EDITION_WORDS})[^)\]\}}]*[\)\]\}}]",
    re.IGNORECASE,
)
_ROMAN_TAIL_RE = re.compile(
    r"(?<=\S)\s+(II|III|IV|VI|VII|VIII|IX|V|X)$", re.IGNORECASE
)
_ROMAN_VALUES = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}

# A tag field cut short is only evidence of the same name when enough of it
# survives: a six-character floor and a 40% ratio keep "Int" from matching
# every name it happens to begin.
_MIN_TRUNCATED_CHARS = 6
_MIN_TRUNCATED_RATIO = 0.4


def fold_for_provider_match(text: str) -> str:
    """Normalize a title or name for comparison against an external catalogue.

    Providers list one record under edition-qualified names ("Abbey Road
    (Remastered)") and spell sequels in roman numerals where a local tag
    uses digits, so both are folded away before similarity is measured.
    Comparison only — the result is never stored or shown.
    """
    if not text:
        return ""
    folded = _EDITION_PAREN_RE.sub(" ", text)
    folded = _WHITESPACE_RUN_RE.sub(" ", folded).strip()
    # Only a trailing numeral that follows a word: an album actually titled
    # "X" or "V" must survive untouched.
    match = _ROMAN_TAIL_RE.search(folded)
    if match:
        folded = folded[: match.start()] + " " + _ROMAN_VALUES[match.group(1).lower()]
    return _WHITESPACE_RUN_RE.sub(" ", folded).strip().casefold()


def name_similarity(left: str, right: str) -> float:
    """Similarity of two names in 0..1, after the provider fold."""
    a, b = fold_for_provider_match(left), fold_for_provider_match(right)
    if not a or not b:
        return 0.0
    return fuzz.ratio(a, b) / 100.0


def truncated_name_similarity(left: str, right: str) -> float:
    """As :func:`name_similarity`, but tolerant of a name cut short.

    Legacy fixed-width tag fields truncate mid-word — "Иванушки Int" for
    "Иванушки International" — and a symmetric ratio counts the missing tail
    against the match, scoring 0.71 where the evidence is actually strong.
    When one folded name is a prefix of the other and enough of it survives,
    the shared prefix decides instead.

    @note Meant for artist names. Album titles are not scored this way: a
        prefix there is as likely to be a different record in a series
        ("Fairytale" against "Fairytale II") as a truncation.
    """
    a, b = fold_for_provider_match(left), fold_for_provider_match(right)
    if not a or not b:
        return 0.0
    ratio = fuzz.ratio(a, b) / 100.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if (
        len(shorter) >= _MIN_TRUNCATED_CHARS
        and len(shorter) / len(longer) >= _MIN_TRUNCATED_RATIO
        and _cut_mid_word(shorter, longer)
    ):
        return max(ratio, fuzz.partial_ratio(a, b) / 100.0)
    return ratio


def _cut_mid_word(shorter: str, longer: str) -> bool:
    """Whether ``shorter`` reads as ``longer`` with its tail cut off.

    Being a prefix is not enough — "The Beatles" also begins "The Beatles
    Revival Band". A fixed-width field stops mid-word and drops nothing
    else, so the last surviving word must be unfinished and nothing may
    follow it.
    """
    short_words, long_words = shorter.split(), longer.split()
    if not short_words or len(short_words) != len(long_words):
        return False
    if short_words[:-1] != long_words[:-1]:
        return False
    last_short, last_long = short_words[-1], long_words[-1]
    return last_short != last_long and last_long.startswith(last_short)
