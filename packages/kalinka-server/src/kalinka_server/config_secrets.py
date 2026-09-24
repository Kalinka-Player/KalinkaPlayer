"""Credentials and private values in the configuration, and the only ways
they may leave the process.

A field declared with the password widget is a credential. The server stores
it and hands it to the plugin that owns it; it never sends it to a client
and never writes it to a log. A client learns only whether one is set.

A field declared ``private`` — and every credential — is shown to clients as
usual but never written to a log: a log line names the field and says it was
updated, and nothing about its value.

The same holds inside a list of records. A credential on an entry is left out
of what a client is sent and named by the entry's id in ``secrets_set``; a
client writing the list back leaves it out in turn, and it is kept while the
entry is still for the same server and user. One it can no longer keep is
reported, since the client cannot tell.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, List
from urllib.parse import quote, quote_plus

from kalinka_plugin_sdk import ConfigRecord
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .config_overrides import model_from_annotation, models_in, to_override_value
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
    return any(
        _holds_private(child)
        for model in models_in(field.annotation)
        for child in model.model_fields.values()
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


def _models_of(value: Any) -> list[BaseModel]:
    if isinstance(value, BaseModel):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, BaseModel)]
    return []


def secret_values(model: BaseModel) -> Iterator[str]:
    """The credentials ``model`` currently holds, nested groups and records
    included."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name, None)
        if is_secret(field):
            if value:
                yield str(value)
            continue
        for nested in _models_of(value):
            yield from secret_values(nested)


def record_key(record: BaseModel, index: int) -> str:
    """How a path names one entry of a list of models: its id when it is a
    :class:`ConfigRecord`, its position otherwise."""
    return record.id if isinstance(record, ConfigRecord) else str(index)


def public_records(
    records: List[BaseModel], path: str, secrets_set: set[str]
) -> list[dict[str, Any]]:
    """A list of models as a client may see it: every credential left out,
    and each one that holds something named in ``secrets_set``."""
    return [
        _public_record(record, f"{path}.{record_key(record, index)}", secrets_set)
        for index, record in enumerate(records)
    ]


def _public_record(
    record: BaseModel, path: str, secrets_set: set[str]
) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for name, field in type(record).model_fields.items():
        if field.exclude:
            continue
        value = getattr(record, name, None)
        child = f"{path}.{name}"
        if is_secret(field):
            if value:
                secrets_set.add(child)
        elif isinstance(value, BaseModel):
            public[name] = _public_record(value, child, secrets_set)
        elif _models_of(value):
            public[name] = public_records(value, child, secrets_set)
        else:
            public[name] = to_override_value(value)
    return public


@dataclass(frozen=True)
class LostCredential:
    """A saved credential that a written entry left out and could not keep.

    The client was never sent it, so it goes on showing one as saved unless
    it is told otherwise.
    """

    # Within the list: ``<id>.<field path>``.
    path: str
    message: str


def keep_unsent_secrets(previous: Any, sent: Any, stored: Any) -> list[LostCredential]:
    """Carry over each credential a client left out of the records it wrote.

    It was never sent one, so a list it writes back holds only the
    credentials it replaced. An entry keeps a saved credential when the
    entry it replaces has the same id, the same type and the same
    :meth:`ConfigRecord.credential_scope`; a credential sent explicitly,
    even as an empty string, is what the client asked for.

    @param previous The list as it was before the write.
    @param sent The list as the client sent it, before validation.
    @param stored The list validated from ``sent``, updated in place.
    @return Each saved credential an entry left out and did not keep.
    """
    if not all(isinstance(v, list) for v in (previous, sent, stored)):
        return []
    saved = {r.id: r for r in previous if isinstance(r, ConfigRecord)}
    lost: list[LostCredential] = []
    for record, raw in zip(stored, sent):
        if not isinstance(record, ConfigRecord) or not isinstance(raw, dict):
            continue
        old = saved.get(record.id)
        if old is None:
            continue
        if (
            type(old) is type(record)
            and old.credential_scope() == record.credential_scope()
        ):
            _carry_secrets(old, record, raw)
        lost.extend(
            LostCredential(f"{record.id}.{path}", message)
            for path, message in _left_behind(old, record, raw)
        )
    return lost


def _left_behind(
    old: BaseModel, new: BaseModel, raw: dict[str, Any], retyped: bool = False
) -> Iterator[tuple[str, str]]:
    """Each credential ``raw`` left out that ``old`` holds and ``new`` did not
    keep, by its path within ``new``, with what to tell the user."""
    retyped = retyped or type(old) is not type(new)
    for name, field in type(new).model_fields.items():
        old_value, new_value = getattr(old, name, None), getattr(new, name, None)
        if is_secret(field):
            if name not in raw and old_value and new_value != old_value:
                was_for = (
                    "another kind of entry" if retyped else "another server or user"
                )
                title = (field.title or name).lower()
                yield name, f"enter the {title} again: the saved one was for {was_for}"
        elif isinstance(new_value, BaseModel) and isinstance(old_value, BaseModel):
            sent = raw.get(name)
            for path, message in _left_behind(
                old_value, new_value, sent if isinstance(sent, dict) else {}, retyped
            ):
                yield f"{name}.{path}", message


def _carry_secrets(old: BaseModel, new: BaseModel, raw: dict[str, Any]) -> None:
    for name, field in type(new).model_fields.items():
        if is_secret(field):
            if name not in raw:
                setattr(new, name, getattr(old, name))
            continue
        old_value, new_value = getattr(old, name, None), getattr(new, name, None)
        if (
            isinstance(new_value, BaseModel)
            and type(old_value) is type(new_value)
            and isinstance(raw.get(name), dict)
        ):
            _carry_secrets(old_value, new_value, raw[name])


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
