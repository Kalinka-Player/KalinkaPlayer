"""Judging configuration changes before they are kept.

``PUT /server/config`` used to be a typed setattr and nothing more, so the
first thing to notice a misspelled folder or a port that is not a number was
whatever broke later. Everything a change can be wrong about is decided here
instead, in two layers: the declared type, which this module checks, and what
the type cannot say — that a folder exists, that a share names more than its
server — which only the plugin that owns the field knows and answers through
``validate_config``.

The verdict is advisory or binding by severity, never by who issued it: an
ERROR refuses the whole batch, a WARNING is reported and applied. Both reach
the settings page as the user types, through the same call the save makes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from kalinka_plugin_sdk import ConfigIssue, IssueSeverity
from pydantic import BaseModel, ValidationError

from .config_overrides import set_by_path
from .config_schema_processor import get_field_value
from .config_secrets import is_secret_path, loggable
from .player_setup import PreparedPlugin

logger = logging.getLogger(__name__.split(".")[-1])


class ConfigWriteError(ValueError):
    """A config write that cannot be attempted at all.

    Distinct from a :class:`ConfigIssue`: an issue is about a value the
    caller may fix and try again, this is about a request that does not
    address the configuration the server is running. ``status_code`` is the
    answer it deserves, kept here so the two write routes cannot disagree
    about it.
    """

    status_code = 400


class ConfigKeyError(ConfigWriteError):
    """A change names nothing that can be written."""


class StaleSchemaError(ConfigWriteError):
    """The caller is working from a schema this server no longer serves."""

    status_code = 409


class DynamicFieldError(ConfigWriteError):
    """A change targets a value the owning plugin resolves for itself."""


def changes_from_payload(
    payload: Any, schema_version: str, dynamic_paths: Iterable[str]
) -> dict[str, Any]:
    """The change map out of a config write body.

    Everything that can be decided without looking at a single value: that
    the body is the right shape, that the caller and the server agree on
    what the settings are, and that nothing is being written where only the
    plugin may write.

    @raise ConfigWriteError If the body cannot be acted on.
    """
    if not isinstance(payload, dict):
        raise ConfigWriteError("Body must be a JSON object")

    client_version = payload.get("schema_version")
    if client_version and client_version != schema_version:
        raise StaleSchemaError(
            "Stale schema_version; refetch /server/config/schema and retry."
        )

    changes = payload.get("changes", payload)
    if not isinstance(changes, dict):
        raise ConfigWriteError("'changes' must be a JSON object")

    dynamic = frozenset(dynamic_paths)
    for key in changes:
        if key in dynamic:
            raise DynamicFieldError(
                f"'{key}' is a dynamic (plugin-resolved) field and cannot be "
                "written via /server/config"
            )
    return changes


@dataclass(frozen=True)
class _Target:
    """Where one change lands: the model, the path within it, and the plugin
    that owns it (None for the server's own configuration)."""

    prefix: str
    model: BaseModel
    attrs: list[str]
    plugin: PreparedPlugin | None


@dataclass(frozen=True)
class ConfigTargets:
    """The three roots a dotted config path can address.

    Holds the live models. Validation never writes to them — it works on
    copies — but resolution has to start from the same objects the apply
    does, or the two would disagree about which keys exist.
    """

    base_config: BaseModel
    input_modules: dict[str, PreparedPlugin]
    devices: dict[str, PreparedPlugin]

    def resolve(self, key: str) -> _Target:
        """Split a dotted path into the model it addresses and the rest.

        @raise ConfigKeyError If the path names no writable field.
        """
        attrs = key.split(".") if isinstance(key, str) else []
        if not attrs or any(p == "" for p in attrs):
            raise ConfigKeyError("Invalid config key")

        root = attrs[0]
        plugin: PreparedPlugin | None = None
        if root == "base_config":
            model: BaseModel | None = self.base_config
            attrs = attrs[1:]
            prefix = "base_config"
        elif root in ("input_modules", "devices"):
            if len(attrs) < 3:
                raise ConfigKeyError("Invalid config key")
            owners = self.input_modules if root == "input_modules" else self.devices
            plugin = owners.get(attrs[1])
            model = plugin.plugin_context.config if plugin is not None else None
            prefix = f"{root}.{attrs[1]}"
            attrs = attrs[2:]
        else:
            raise ConfigKeyError("Invalid config key")

        if model is None or not attrs:
            raise ConfigKeyError("Invalid config key")
        if attrs[0] == "name":
            raise ConfigKeyError("Cannot modify 'name' field")
        return _Target(prefix=prefix, model=model, attrs=attrs, plugin=plugin)


def _first_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False)
    if errors and errors[0].get("msg"):
        return str(errors[0]["msg"])
    return "the value is not valid for this setting"


def apply_change(model: BaseModel, attrs: list[str], value: Any) -> str | None:
    """Write one value onto ``model``, as its declared type.

    The same call serves the dry run and the save: the dry run writes to a
    copy, the save to the live configuration, and neither may accept what
    the other would refuse.

    @return The reason the value cannot be stored, in words for the user,
        or None when it was written.
    @raise ConfigKeyError If the path names no field.
    """
    try:
        set_by_path(model, attrs, value)
    except ValidationError as exc:
        return _first_message(exc)
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ConfigKeyError("Invalid config key") from exc
    return None


def commit_change(target: _Target, key: str, value: Any) -> str | None:
    """Write one change onto the live configuration and log it, showing a
    credential only as redacted.

    @return As :func:`apply_change`.
    @raise ConfigKeyError As :func:`apply_change`.
    """
    secret = is_secret_path(type(target.model), target.attrs)
    logger.info("Setting config field %s to %s", key, loggable(value, secret))
    reason = apply_change(target.model, target.attrs, value)
    if reason is None:
        logger.info(
            "Set %s to %s, saved: %s",
            key,
            loggable(value, secret),
            loggable(get_field_value(target.model, target.attrs), secret),
        )
    return reason


async def _plugin_issues(
    plugin: PreparedPlugin, candidate: BaseModel, changed: frozenset[str], prefix: str
) -> list[ConfigIssue]:
    instance = plugin.plugin_instance
    if instance is None:
        return []
    try:
        issues = await instance.validate_config(candidate, changed)
        return [
            issue.model_copy(update={"path": _full_path(prefix, issue.path)})
            for issue in issues or ()
        ]
    except Exception as exc:  # noqa: BLE001 — a broken validator must not lock the page
        logger.exception(
            "Plugin %s failed to validate its configuration: %s", prefix, exc
        )
        return []


def _full_path(prefix: str, relative: str) -> str:
    """A plugin's own path under the module it belongs to. A plugin that
    names no field is talking about the module itself, and gets the prefix
    rather than a path ending in a dot that nothing would match."""
    return f"{prefix}.{relative}" if relative else prefix


async def validate_changes(
    changes: Mapping[str, Any], targets: ConfigTargets
) -> list[ConfigIssue]:
    """Everything wrong with ``changes``, in full dotted paths.

    Type failures are found here; anything further is the owning plugin's to
    say. Each plugin is asked once, about a copy of its configuration with
    every change in this batch already applied, because a value can be
    acceptable beside one change and not beside another — a share is only
    reachable with the password that arrived in the same save.

    @raise ConfigKeyError If any key names nothing writable. The batch is
        the caller's to reject; nothing here has been written.
    """
    groups: dict[str, tuple[_Target, BaseModel, set[str]]] = {}
    issues: list[ConfigIssue] = []

    for key, value in changes.items():
        target = targets.resolve(key)
        if target.prefix not in groups:
            groups[target.prefix] = (
                target,
                target.model.model_copy(deep=True),
                set(),
            )
        _, candidate, changed = groups[target.prefix]
        reason = apply_change(candidate, target.attrs, value)
        if reason is not None:
            issues.append(
                ConfigIssue(path=key, message=reason, severity=IssueSeverity.ERROR)
            )
            continue
        changed.add(".".join(target.attrs))

    for prefix, (target, candidate, changed) in groups.items():
        if target.plugin is None or not changed:
            continue
        issues.extend(
            await _plugin_issues(target.plugin, candidate, frozenset(changed), prefix)
        )
    return issues


def blocking(issues: Iterable[ConfigIssue]) -> list[ConfigIssue]:
    """The issues that refuse the save."""
    return [i for i in issues if i.severity == IssueSeverity.ERROR]
