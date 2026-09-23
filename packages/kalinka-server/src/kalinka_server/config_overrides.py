"""User configuration overrides.

A single JSON file holds only the values the user has explicitly set,
keyed by the same dotted paths the ``/server/config`` PUT endpoint
accepts (``base_config.*``, ``input_modules.<name>.*``,
``devices.<name>.*``). Everything not in this file falls back to the
code defaults, so default-value changes ship cleanly with a new release.

Loaded once at startup and rewritten whenever the PUT endpoint mutates
state. Atomic-write via tempfile + ``os.replace`` so a crash mid-write
cannot leave a half-written file in place.
"""

import json
import logging
import os
import tempfile
from typing import Annotated, Any, Dict, List, Mapping, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__.split(".")[-1])


def coerce_field_value(owner: BaseModel, field: str, value: Any) -> Any:
    """``value`` as the declared type of ``owner.field``, constraints and all.

    A plain setattr would silently store a type-invalid value (e.g.
    port="abc") as the wrong type and blow it up later in unrelated code.
    Coercion also normalises JSON-decoded values ("9001" -> 9001), which is
    what every value arriving from the overrides file or the config PUT has
    been through.

    @note The bounds declared with ``Field(ge=..., max_length=...)`` live
        beside the annotation rather than in it, so they are put back before
        validating — no config model sets ``validate_assignment``, which
        makes this the only thing standing between a PUT and an
        out-of-range value on the live configuration.

    @raise pydantic.ValidationError If the value is not that type and
        cannot be made into it.
    """
    fields = getattr(type(owner), "model_fields", None)
    if fields is None or field not in fields:
        return value
    info = fields[field]
    if info.annotation is None:
        return value
    declared = (
        Annotated[tuple([info.annotation, *info.metadata])]
        if info.metadata
        else info.annotation
    )
    return TypeAdapter(declared).validate_python(value)


def describe_failure(exc: Exception) -> str:
    """Why a value could not be stored, in words that never quote the value.

    A ValidationError's own text does quote it, and the value may be a
    credential.
    """
    if isinstance(exc, ValidationError):
        return "; ".join(e["msg"] for e in exc.errors(include_input=False))
    return str(exc)


def set_by_path(model: BaseModel, attrs: List[str], value: Any) -> None:
    """Write ``value`` at a dotted path, as the declared type of the field it
    lands on.

    @raise pydantic.ValidationError If the value is not that type.
    @raise AttributeError If a component of the path names no attribute.
    @raise ValueError If the last component names no field on the model it
        lands on — which is what pydantic raises for that, not
        ``AttributeError``.
    """
    current: Any = model
    for part in attrs[:-1]:
        current = getattr(current, part)
    field = attrs[-1]
    setattr(current, field, coerce_field_value(current, field, value))


def load_overrides(path: str) -> Dict[str, Any]:
    """Read overrides from disk. Missing/unreadable file yields ``{}``."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Cannot read overrides file %s: %s. Starting with defaults.",
            path,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Overrides file %s is not a JSON object. Ignoring.", path
        )
        return {}
    return data


def save_overrides(path: str, overrides: Mapping[str, Any]) -> None:
    """Atomically rewrite the overrides file."""
    config_dir = os.path.dirname(path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".overrides-", suffix=".tmp", dir=config_dir or "."
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(dict(overrides), f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_path(model: BaseModel, attrs: List[str]) -> Any:
    current: Any = model
    for part in attrs:
        current = getattr(current, part)
    return current


def model_from_annotation(annotation: Any) -> type[BaseModel] | None:
    """Resolve a field annotation to its BaseModel class, unwrapping
    Optional/Union (e.g. ``SubModel | None`` → ``SubModel``). None if the
    annotation isn't (or doesn't wrap) a BaseModel."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def field_info(model_cls: type[BaseModel], attrs: List[str]) -> FieldInfo | None:
    """The declaration of the field at dotted ``attrs`` on ``model_cls``, or
    None when the path names no field. Walks nested pydantic models
    (including Optional ones) so ``sub.flag`` works too."""
    cls: Any = model_cls
    info = None
    for part in attrs:
        fields = getattr(cls, "model_fields", None)
        if not fields or part not in fields:
            return None
        info = fields[part]
        cls = model_from_annotation(info.annotation)
    return info


def is_one_shot_field(model_cls: type[BaseModel], attrs: List[str]) -> bool:
    """True if the field at dotted ``attrs`` on ``model_cls`` is declared a
    one-shot trigger via ``json_schema_extra={"one_shot": True}``.

    A one-shot field is a "do X once on the next restart" toggle: set via the
    normal config PUT, acted on a single time at startup, then reset by the
    framework.
    """
    info = field_info(model_cls, attrs)
    if info is None:
        return False
    extra = info.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get("one_shot"))


def find_one_shot_overrides(
    model_cls: type[BaseModel],
    prefix: str,
    config: BaseModel,
    overrides: Mapping[str, Any],
) -> List[str]:
    """Return the override keys under ``prefix`` that target a one-shot field
    and are currently *armed* — i.e. their value on ``config`` differs from
    the field's default.

    Pure (no mutation): the caller resets these persist-first before the
    plugin acts, so an armed trigger fires at most once and never repeats on
    the following boot.
    """
    default = model_cls()
    armed: List[str] = []
    for key in overrides:
        if not key.startswith(prefix):
            continue
        attrs = key[len(prefix):].split(".")
        if not is_one_shot_field(model_cls, attrs):
            continue
        try:
            if _read_path(config, attrs) != _read_path(default, attrs):
                armed.append(key)
        except (AttributeError, IndexError, TypeError):
            continue
    return armed


def apply_overrides_with_prefix(
    model: BaseModel,
    overrides: Mapping[str, Any],
    prefix: str,
) -> None:
    """Apply every override whose key starts with ``prefix`` to ``model``.

    Invalid paths or values are logged and skipped — the overrides file
    can outlive schema renames, and we'd rather start with the wrong
    value missing than refuse to boot.
    """
    for key, value in overrides.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if not suffix:
            continue
        attrs = suffix.split(".")
        try:
            set_by_path(model, attrs, value)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            logger.warning(
                "Ignoring override '%s' (cannot apply): %s",
                key,
                describe_failure(exc),
            )
