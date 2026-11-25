"""Parts of a search query that name metadata rather than sound.

CLAP hears what music sounds like, not when it was released or where it comes
from, so a decade ("70s prog rock") or an origin ("italian pop") in the query
is read here and applied as a filter on the library's metadata, while the
whole query still goes to CLAP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_WORDS = {
    "twenties": 1920,
    "thirties": 1930,
    "forties": 1940,
    "fifties": 1950,
    "sixties": 1960,
    "seventies": 1970,
    "eighties": 1980,
    "nineties": 1990,
    "noughties": 2000,
}

# "1970s", "70s", "'70s", "70's"; a lone "70" or a year like "1975" is not a decade.
_DECADE_RE = re.compile(r"(?<![\w])['’]?((?:19|20)\d0|\d0)['’]?s\b", re.IGNORECASE)
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.IGNORECASE)
# "top 40s" is a chart and "my 20s" an age, not a decade.
_NOT_A_DECADE_AFTER = re.compile(r"\b(?:top|my|your|his|her|our|their)\s+$", re.IGNORECASE)


def _century(two_digits: int) -> int:
    """The decade a two-digit "Ns" names: 00s and 10s are this century's,
    20s through 90s the last one's, as music talk uses them."""
    return 2000 + two_digits if two_digits < 20 else 1900 + two_digits


def decades_in(query: str) -> list[int]:
    """The decades ``query`` names, as their first years, in the order named.

    @return Empty when the query names none.
    """
    found: list[tuple[int, int]] = []
    for match in _DECADE_RE.finditer(query):
        if _NOT_A_DECADE_AFTER.search(query, 0, match.start()):
            continue
        digits = match.group(1)
        decade = int(digits) if len(digits) == 4 else _century(int(digits))
        found.append((match.start(), decade))
    for match in _WORD_RE.finditer(query):
        found.append((match.start(), _WORDS[match.group(1).lower()]))
    return list(dict.fromkeys(decade for _, decade in sorted(found)))


@dataclass(frozen=True)
class Origin:
    """Where music comes from, as a query names it: the language it is sung
    in, as an ISO 639-3 code, and the countries its artists come from, as
    ISO 3166-1 alpha-2 codes. Either may be missing: "american" names no
    language, "arabic" no country."""

    language: Optional[str]
    countries: frozenset[str]


def _origin(language: Optional[str], *countries: str) -> Origin:
    return Origin(language, frozenset(countries))


_ORIGINS = {
    "american": _origin(None, "US"),
    "arabic": _origin("ara"),
    "argentine": _origin(None, "AR"),
    "argentinian": _origin(None, "AR"),
    "australian": _origin(None, "AU"),
    "austrian": _origin(None, "AT"),
    "belgian": _origin(None, "BE"),
    "brazilian": _origin(None, "BR"),
    "british": _origin(None, "GB"),
    "canadian": _origin(None, "CA"),
    "chinese": _origin("zho", "CN"),
    "cuban": _origin(None, "CU"),
    "czech": _origin("ces", "CZ"),
    "danish": _origin("dan", "DK"),
    "dutch": _origin("nld", "NL"),
    "english": _origin("eng", "GB"),
    "finnish": _origin("fin", "FI"),
    "french": _origin("fra", "FR"),
    "german": _origin("deu", "DE"),
    "greek": _origin("ell", "GR"),
    "hebrew": _origin("heb"),
    "hungarian": _origin("hun", "HU"),
    "icelandic": _origin("isl", "IS"),
    "indian": _origin(None, "IN"),
    "irish": _origin("gle", "IE"),
    "israeli": _origin(None, "IL"),
    "italian": _origin("ita", "IT"),
    "italo": _origin("ita", "IT"),
    "jamaican": _origin(None, "JM"),
    "japanese": _origin("jpn", "JP"),
    "korean": _origin("kor", "KR"),
    "mexican": _origin(None, "MX"),
    "nigerian": _origin(None, "NG"),
    "norwegian": _origin("nor", "NO"),
    "polish": _origin("pol", "PL"),
    "portuguese": _origin("por", "PT"),
    "russian": _origin("rus", "RU"),
    "soviet": _origin(None, "SU"),
    "spanish": _origin("spa", "ES"),
    "swedish": _origin("swe", "SE"),
    "swiss": _origin(None, "CH"),
    "turkish": _origin("tur", "TR"),
    "ukrainian": _origin("ukr", "UA"),
}

_ORIGIN_RE = re.compile(r"\b(" + "|".join(_ORIGINS) + r")\b", re.IGNORECASE)
# Names of a wider region or a people that contain an origin's word but not its country.
_NOT_AN_ORIGIN = re.compile(
    r"\b(?:(?:latin|south|central|north|african|afro|pan)[\s-]+american|west[\s-]+indian)\b",
    re.IGNORECASE,
)
# "french horn" is an instrument and "italian concerto" a work, not where the music is from.
_NOT_AN_ORIGIN_BEFORE = re.compile(
    r"\s+(?:horns?|guitars?|suites?|concert(?:o|os|i)|dances?|march(?:es)?|rhapsod(?:y|ies)"
    r"|requiem|symphon(?:y|ies)|overtures?|serenades?|sonatas?|capriccio|caprice)\b",
    re.IGNORECASE,
)


def origins_in(query: str) -> list[Origin]:
    """The origins ``query`` names, in the order named.

    @return Empty when the query names none.
    """
    query = _NOT_AN_ORIGIN.sub(" ", query)
    found = [
        _ORIGINS[match.group(1).lower()]
        for match in _ORIGIN_RE.finditer(query)
        if not _NOT_AN_ORIGIN_BEFORE.match(query, match.end())
    ]
    return list(dict.fromkeys(found))
