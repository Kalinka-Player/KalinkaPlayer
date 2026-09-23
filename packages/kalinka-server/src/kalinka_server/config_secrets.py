"""Credentials and private values in the configuration, and the only ways
they may leave the process.

A field declared with the password widget is a credential. The server stores
it and hands it to the plugin that owns it; it never sends it to a client
and never writes it to a log. A client learns only whether one is set.

A field declared ``private`` — and every credential — is shown to clients as
usual but never written to a log: a log line names the field and says it was
updated, and nothing about its value.
"""

import re
from typing import Any, Callable, Iterable, Iterator, List
from urllib.parse import quote, quote_plus

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .config_overrides import model_from_annotation
from .credential_names import is_credential_name
from .presentation_schema import Widget

#: What a log line shows in place of a credential.
REDACTED = "<secret>"

# To the last "@": over-masking is safe, stopping at a "/" in a password is not.
# The scheme is bounded, or a long word would be rescanned from every letter.
_URL_PASSWORD_RE = re.compile(r"(\w[\w+.\-]{0,31}://[^\s/:@]*):(\S*)@")

_WORD_RE = re.compile(r"\S+")

_PLACEHOLDERS = frozenset(("", REDACTED, "none", "null", "true", "false"))

# A name, then ":" or "=", then its value, quoted or not: a query parameter, a
# header, a dict or JSON pair. The name starts a word, so a long word is not
# rescanned from every letter; the value is only peeked at, one character past
# the longest placeholder, so a pair inside it is still found.
_PAIR_RE = re.compile(
    r"(?<![\w.\-])([\w.\-]+)[\"']?\s*[:=]\s*[\"']?"
    rf"(?=([^\s\"',;}}&]{{0,{max(map(len, _PLACEHOLDERS)) + 1}}}))"
)


def _mask_url_password_in_word(found: "re.Match[str]") -> str:
    word = found.group(0)
    # Only up to the last "@", so a scheme with none after it fails at once.
    end = word.rfind("@") + 1
    if not end:
        return word
    return _URL_PASSWORD_RE.sub(rf"\1:{REDACTED}@", word[:end]) + word[end:]


def _mask_url_passwords(text: str) -> str:
    if "@" not in text:
        return text
    return _WORD_RE.sub(_mask_url_password_in_word, text)


def _has_url_password(text: str) -> bool:
    if "@" not in text:
        return False
    for word in _WORD_RE.findall(text):
        end = word.rfind("@") + 1
        found = _URL_PASSWORD_RE.search(word[:end]) if end else None
        if found is not None and found.group(2) != REDACTED:
            return True
    return False


def _has_credential_pair(text: str) -> bool:
    for found in _PAIR_RE.finditer(text):
        value = found.group(2)
        if value.lower() in _PLACEHOLDERS or not value.strip("*"):
            continue
        in_query = found.start() > 0 and text[found.start() - 1] in "?&;"
        if is_credential_name(found.group(1), in_query):
            return True
    return False


def is_secret(field: FieldInfo) -> bool:
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get("widget") == Widget.PASSWORD.value


def is_private(field: FieldInfo) -> bool:
    """Whether a log may never show the field's value: a credential, or a
    field declared ``json_schema_extra={"private": True}`` — a host, a path, a
    user name. A log names such a field and says it changed, nothing more."""
    extra = field.json_schema_extra
    return is_secret(field) or (isinstance(extra, dict) and extra.get("private") is True)


def _holds_private(field: FieldInfo) -> bool:
    if is_private(field):
        return True
    model = model_from_annotation(field.annotation)
    return model is not None and any(
        _holds_private(child) for child in model.model_fields.values()
    )


def is_private_path(model_cls: type[BaseModel], attrs: List[str]) -> bool:
    """Whether dotted ``attrs`` on ``model_cls`` is private: the field is, a
    group it sits in is, or it is a group with a private field anywhere
    inside. A path that names no field counts as private, since nothing
    vouches for what it holds."""
    cls: Any = model_cls
    info = None
    for part in attrs:
        fields = getattr(cls, "model_fields", None)
        if not fields or part not in fields:
            return True
        info = fields[part]
        if is_private(info):
            return True
        cls = model_from_annotation(info.annotation)
    return info is None or _holds_private(info)


def loggable(value: Any) -> str:
    """``value`` as a log line may show it, without a password written into a
    URL.

    @note Only for a value :func:`is_private_path` lets a log show.
    """
    return _mask_url_passwords(repr(value))


def secret_values(model: BaseModel) -> Iterator[str]:
    """The credentials ``model`` currently holds, nested groups included."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name, None)
        if isinstance(value, BaseModel):
            yield from secret_values(value)
        elif is_secret(field) and value:
            yield str(value)


def credential_detector(secrets: Iterable[str]) -> Callable[[str], bool]:
    """A test for whether a line of log text carries a credential.

    True when it holds one of ``secrets``, also percent-encoded, or anything
    that looks like one: a password written into a URL, or a credential-named
    header, query parameter or key-value pair with a value. It errs towards
    true — a line it flags is left out whole, never partly masked.

    @note The returned function keeps the secrets in memory only; it is safe
        to call from any thread.
    """
    forms = {
        form
        for secret in secrets
        for form in (secret, quote(secret, safe=""), quote_plus(secret, safe=""))
    }
    known = (
        re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)))
        if forms
        else None
    )

    def carries_credential(text: str) -> bool:
        return (
            (known is not None and known.search(text) is not None)
            or _has_url_password(text)
            or _has_credential_pair(text)
        )

    return carries_credential
