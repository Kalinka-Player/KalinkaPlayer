"""Registry of dynamic-option resolvers for enum-like fields.

A handful of settings have a writable enum choice whose option list
depends on live system state (ALSA devices that come and go on
hot-plug, network interfaces, COM ports, ...). Hard-coding their
choices in the schema would either be stale or churn the
``schema_version`` on every transient hardware change. Instead such a
field leaves ``enum_values`` empty in the schema (see ``FieldSpec``);
this module keeps a separate map of ``path -> callable`` whose results
are spliced into the ``GET /server/config`` envelope under
``enum_options[path]``. Clients prefer envelope options when present
and fall back to ``enum_values`` otherwise, so no per-field flag is
needed.

The resolver is a plain callable (sync or async) that returns a list
of ``OptionSpec`` dicts. Failures are caught at the registry boundary
so a broken enumerator can't take the whole values blob down with it.

Parallel to ``dynamic_field_registry``: that one is for *value*
resolution of read-only status views; this one is for *option*
resolution of writable fields. They never overlap.

Plugins reach the registry through their config model rather than
through a registration call: a field tagged
``json_schema_extra={"dynamic_options": True}`` is bound to its
plugin's ``resolve_options`` by :func:`register_plugin_options` at
startup.
"""

from __future__ import annotations

import inspect
import logging
from functools import partial
from typing import Any, Awaitable, Callable, Union

from pydantic import BaseModel, ValidationError

from kalinka_plugin_sdk.plugin import PluginBase

from .player_setup import PreparedPlugin
from .presentation_schema import OptionSpec, PresentationSchema


logger = logging.getLogger(__name__.split(".")[-1])


# A resolver may be sync or async; the registry handles both. It must
# return an iterable of OptionSpec (or anything OptionSpec can absorb).
OptionResolver = Callable[
    [], Union[list[OptionSpec], Awaitable[list[OptionSpec]]]
]


class OptionsRegistry:
    """Process-wide map of dotted config paths to option resolvers.

    Mutable so the server can register entries during startup (after
    plugins are loaded) and consult them per-request. Resolvers
    captured here outlive any single request — they're expected to be
    cheap and side-effect-free, performing only system queries.
    """

    def __init__(self) -> None:
        self._resolvers: dict[str, OptionResolver] = {}

    def register(self, path: str, resolver: OptionResolver) -> None:
        if path in self._resolvers:
            logger.warning(
                "Replacing existing option resolver for %s", path
            )
        self._resolvers[path] = resolver

    def paths(self) -> list[str]:
        return list(self._resolvers)

    async def resolve(self, path: str) -> list[OptionSpec] | None:
        resolver = self._resolvers.get(path)
        if resolver is None:
            return None
        try:
            result = resolver()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.exception(
                "Option resolver for %s raised; omitting from response: %s",
                path,
                exc,
            )
            return None
        return _coerce_options(result)


def _coerce_options(raw: Any) -> list[OptionSpec]:
    """Accept anything resolver returns (list of OptionSpec, list of
    dicts, list of (value, label) tuples) and normalise.
    """
    out: list[OptionSpec] = []
    for entry in raw or ():
        if isinstance(entry, OptionSpec):
            out.append(entry)
            continue
        # A plugin answers in the SDK's own option type, which carries the
        # same three fields under a class the server does not import.
        if isinstance(entry, BaseModel):
            entry = entry.model_dump()
        # A malformed entry (e.g. dict missing the required value/label)
        # makes OptionSpec(...) raise ValidationError. Skip just that
        # entry rather than letting one bad option take the whole
        # response down — the registry boundary is meant to be defensive.
        try:
            if isinstance(entry, dict):
                out.append(OptionSpec(**entry))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                out.append(OptionSpec(value=str(entry[0]), label=str(entry[1])))
            else:
                logger.warning(
                    "Option resolver returned unsupported entry %r; skipping",
                    entry,
                )
        except ValidationError as exc:
            logger.warning(
                "Option resolver returned invalid entry %r; skipping: %s",
                entry,
                exc,
            )
    return out


def _owner_of(
    path: str,
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
) -> tuple[PluginBase, str, str] | None:
    """The plugin that answers for a dotted config path, its id, and the
    path as that plugin spells it. None when no loaded plugin owns it."""
    parts = path.split(".")
    if len(parts) < 3:
        return None
    prepared = {"input_modules": input_modules, "devices": devices}.get(parts[0])
    if prepared is None:
        return None
    plugin = prepared.get(parts[1])
    if plugin is None or plugin.plugin_instance is None:
        return None
    return plugin.plugin_instance, parts[1], ".".join(parts[2:])


async def _resolve_options(
    instance: PluginBase, plugin_id: str, subpath: str
) -> list[Any]:
    """One field's suggestions, asked of the plugin that owns it."""
    try:
        return list(await instance.resolve_options(subpath))
    except KeyError as exc:
        # Only the refusal the SDK documents — KeyError(path) — is the
        # plugin saying it does not own the field. A KeyError from a dict
        # inside the resolver means something else entirely, and blaming
        # the declaration for it hides the real fault.
        if exc.args != (subpath,):
            raise
        logger.warning(
            "Plugin %s declares suggestions for %r but does not resolve it",
            plugin_id,
            subpath,
        )
        return []


def register_plugin_options(
    registry: OptionsRegistry,
    schema: PresentationSchema,
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
) -> None:
    """Bind every field tagged ``dynamic_options`` to its plugin's resolver.

    Reads the built schema rather than walking the config models again, so
    a field is offered suggestions on exactly the terms the settings page
    renders it — including the widget guard that drops the tag where a
    suggestion could not be shown.
    """
    for field in schema.expert_fields:
        if not field.dynamic_options:
            continue
        owner = _owner_of(field.path, input_modules, devices)
        if owner is None:
            logger.warning(
                "Field %s is tagged dynamic_options but no loaded plugin owns "
                "it; no suggestions will be offered",
                field.path,
            )
            continue
        registry.register(field.path, partial(_resolve_options, *owner))
