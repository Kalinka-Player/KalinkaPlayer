"""Credentials in the configuration, and the only ways they may leave the process.

A field declared with the password widget is a credential. The server stores
it and hands it to the plugin that owns it; it never sends it to a client
and never writes it to a log. A client learns only whether one is set.
"""

import re
from typing import Any, List

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .config_overrides import field_info, model_from_annotation
from .presentation_schema import Widget

#: What a log line shows in place of a credential.
REDACTED = "<secret>"

# To the last "@": over-masking is safe, stopping at a "/" in a password is not.
_URL_PASSWORD_RE = re.compile(r"(\w[\w+.\-]*://[^\s/:@]*):\S*@")


def is_secret(field: FieldInfo) -> bool:
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get("widget") == Widget.PASSWORD.value


def _holds_secret(field: FieldInfo) -> bool:
    if is_secret(field):
        return True
    model = model_from_annotation(field.annotation)
    return model is not None and any(
        _holds_secret(child) for child in model.model_fields.values()
    )


def is_secret_path(model_cls: type[BaseModel], attrs: List[str]) -> bool:
    """Whether dotted ``attrs`` on ``model_cls`` names a credential, or a group
    with one anywhere inside it."""
    info = field_info(model_cls, attrs)
    return info is not None and _holds_secret(info)


def loggable(value: Any, secret: bool) -> str:
    """``value`` as a log line may show it: a credential not at all, and
    anything else without a password written into a URL."""
    if secret:
        return REDACTED
    return _URL_PASSWORD_RE.sub(rf"\1:{REDACTED}@", repr(value))
