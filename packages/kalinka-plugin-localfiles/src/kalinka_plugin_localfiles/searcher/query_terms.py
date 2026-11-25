"""Parts of a search query that name metadata rather than sound.

CLAP hears what music sounds like, not when it was released, so a decade in
the query ("70s prog rock") is read here and applied as a filter on the
album's year, while the whole query still goes to CLAP.
"""

from __future__ import annotations

import re

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


def year_spans(decades: list[int]) -> list[tuple[int, int]]:
    """Each decade as (first year, last year)."""
    return [(decade, decade + 9) for decade in decades]
