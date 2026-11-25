"""Language names as a library's tags carry them, read as one code each.

MusicBrainz names a release's language by its ISO 639-3 code ("rus"); a
file's own tags name it however the tagger wrote it: "ru", "Russian", the
bibliographic "ger" for German. Each form is read here as the ISO 639-3 code,
so two tracks tagged differently in one language still speak the same one.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

#: ISO 639-3 code, and the other forms a tag may name that language by.
_FORMS: dict[str, tuple[str, ...]] = {
    "ara": ("ar", "arabic"),
    "bel": ("be", "belarusian"),
    "bul": ("bg", "bulgarian"),
    "cat": ("ca", "catalan"),
    "ces": ("cs", "cze", "czech", "čeština"),
    "cym": ("cy", "wel", "welsh"),
    "dan": ("da", "danish", "dansk"),
    "deu": ("de", "ger", "german", "deutsch"),
    "ell": ("el", "gre", "greek"),
    "eng": ("en", "english"),
    "est": ("et", "estonian"),
    "fas": ("fa", "per", "persian", "farsi"),
    "fin": ("fi", "finnish", "suomi"),
    "fra": ("fr", "fre", "french", "français", "francais"),
    "gle": ("ga", "irish"),
    "heb": ("he", "hebrew"),
    "hin": ("hi", "hindi"),
    "hrv": ("hr", "croatian"),
    "hun": ("hu", "hungarian", "magyar"),
    "ind": ("id", "indonesian"),
    "isl": ("is", "ice", "icelandic", "íslenska"),
    "ita": ("it", "italian", "italiano"),
    "jpn": ("ja", "japanese", "日本語"),
    "kor": ("ko", "korean", "한국어"),
    "lat": ("la", "latin"),
    "lav": ("lv", "latvian"),
    "lit": ("lt", "lithuanian"),
    "nld": ("nl", "dut", "dutch", "nederlands"),
    "nor": ("no", "nb", "nn", "nob", "nno", "norwegian", "norsk"),
    "pol": ("pl", "polish", "polski"),
    "por": ("pt", "portuguese", "português", "portugues"),
    "ron": ("ro", "rum", "romanian"),
    "rus": ("ru", "russian", "русский"),
    "slk": ("sk", "slo", "slovak"),
    "slv": ("sl", "slovenian"),
    "spa": ("es", "spanish", "español", "espanol", "castellano"),
    "srp": ("sr", "serbian"),
    "swe": ("sv", "swedish", "svenska"),
    "tha": ("th", "thai"),
    "tur": ("tr", "turkish", "türkçe"),
    "ukr": ("uk", "ukrainian", "українська"),
    "vie": ("vi", "vietnamese"),
    "zho": ("zh", "chi", "cmn", "yue", "chinese", "mandarin", "cantonese", "中文"),
}

_CODE_OF = {form: code for code, forms in _FORMS.items() for form in (code, *forms)}


def canonical_language(raw: Optional[str]) -> Optional[str]:
    """The ISO 639-3 code of the language ``raw`` names.

    @return None when ``raw`` names no single language this table knows,
        which includes MusicBrainz's "mul" (several) and "zxx" (no words).
    """
    if not raw:
        return None
    return _CODE_OF.get(unicodedata.normalize("NFC", raw).strip().casefold())


def language_spellings(code: str) -> list[str]:
    """Each form of the language ``code`` names, as written in lower case,
    upper case and capitalised: SQL matches them as they are, its lower()
    folding no letter outside ASCII."""
    forms = (code, *_FORMS.get(code, ()))
    return list(dict.fromkeys(v for form in forms for v in (form, form.upper(), form.capitalize())))
