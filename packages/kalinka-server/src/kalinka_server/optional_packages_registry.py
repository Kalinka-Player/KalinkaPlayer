"""Runtime registry for plugin-declared optional packages.

Each plugin class may declare `OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec]`
as a class attribute. This module collects them from the currently loaded
plugins (input modules + devices), probes whether each is importable, and
serves the catalog used by the settings UI.

Install requests go through this module: keys are validated against the
in-memory registry and written to /var/lib/kalinka/pending_installs.json.
A settings save, a restart and the server's own start queue what the
plugins need then, so whatever starts the server next installs it.
Bootstrap re-validates
against the deb-shipped manifest at
/opt/kalinka/allowed_packages/<plugin>.json before invoking pip — i.e.
this in-process check is convenience, not the security boundary.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalinka_plugin_sdk import ModuleState, OptionalPackageSpec, wheel_architecture
from kalinka_plugin_sdk import paths

from . import no_wheel_record
from .module_timeout import PLUGIN_CALL_TIMEOUT_S
from .player_setup import PreparedPlugin


logger = logging.getLogger(__name__.split(".")[-1])


PENDING_INSTALLS_FILE = "pending_installs.json"
LAST_INSTALL_FILE = "last_install.json"
PENDING_SCHEMA_VERSION = 1


def _state_file(name: str) -> Path:
    # Per call: $KALINKA_PREFIX may be set after this module is imported.
    return Path(paths.state_dir()) / name


def _is_installed(spec: OptionalPackageSpec, key: str) -> bool:
    import_name = spec.import_name or key
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def _iter_plugin_entries(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
):
    for plugin_id, prepared in input_modules.items():
        yield "input_module", plugin_id, prepared
    for plugin_id, prepared in devices.items():
        yield "device", plugin_id, prepared


Offers = dict[str, list[tuple[str, OptionalPackageSpec]]]


def _collect_offers(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
) -> Offers:
    """{key: [(plugin_id, spec), ...]} for every loaded plugin offering the
    key, in load order."""
    offers: Offers = {}
    for _kind, plugin_id, prepared in _iter_plugin_entries(input_modules, devices):
        plugin_class = prepared.plugin_class
        declared = getattr(plugin_class, "OPTIONAL_PACKAGES", None) or {}
        for key, spec in declared.items():
            if not isinstance(spec, OptionalPackageSpec):
                logger.warning(
                    "Plugin %s declared %r as %r; expected OptionalPackageSpec",
                    plugin_id,
                    key,
                    type(spec).__name__,
                )
                continue
            offers.setdefault(key, []).append((plugin_id, spec))
    return offers


def _terms(key: str, spec: OptionalPackageSpec) -> tuple:
    return (spec.pip_spec, spec.import_name or key, spec.architectures)


def _first_offers(offers: Offers) -> dict[str, tuple[str, OptionalPackageSpec]]:
    registry: dict[str, tuple[str, OptionalPackageSpec]] = {}
    for key, offered in offers.items():
        first_plugin_id, first = offered[0]
        for plugin_id, spec in offered[1:]:
            if _terms(key, spec) != _terms(key, first):
                logger.warning(
                    "Optional package key %r declared by both %s and %s "
                    "with different (pip_spec, import_name, architectures) "
                    "(%r vs %r); first declarer wins",
                    key,
                    first_plugin_id,
                    plugin_id,
                    _terms(key, first),
                    _terms(key, spec),
                )
        registry[key] = (first_plugin_id, first)
    return registry


def build_registry(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
) -> dict[str, tuple[str, OptionalPackageSpec]]:
    """Return {key: (declaring_plugin_id, spec)} from all loaded plugins.

    Plugins may share a key, as two that use the same runtime do. The first
    declarer wins; a warning is logged when the others ask for another
    pip_spec, probe another import name or list other architectures.
    """
    return _first_offers(_collect_offers(input_modules, devices))


def _installable_here(
    registry: dict[str, tuple[str, OptionalPackageSpec]],
) -> dict[str, tuple[str, OptionalPackageSpec]]:
    """The registry without the packages that have no wheel for this box,
    which an install could only fail on, at every restart."""
    architecture = wheel_architecture()
    return {
        key: offer
        for key, offer in registry.items()
        if offer[1].has_wheels_for(architecture)
    }


def _read_last_install(last_install_path: Path) -> Any:
    if not last_install_path.is_file():
        return None
    try:
        return json.loads(last_install_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cannot read last install at %s: %s", last_install_path, e)
        return None


@dataclass(frozen=True)
class InstallHistory:
    """What the installs at earlier starts left for the server.

    These files change only while the server is down, before it starts;
    they are read with the registry, at start and whenever a plugin is set
    up or taken down.
    """

    #: {key: {"pip_spec", "reason"}} for each package pip found no wheel of
    #: for this Python and architecture, at any install so far.
    no_wheel: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: The keys the last install was asked for and did not install, or was
    #: cut off before it finished.
    not_installed: frozenset[str] = frozenset()

    @classmethod
    def read(cls, state_dir: Path | None = None) -> InstallHistory:
        state_dir = state_dir or Path(paths.state_dir())
        last_install = _read_last_install(state_dir / LAST_INSTALL_FILE)
        if isinstance(last_install, dict):
            results = last_install.get("results")
        else:
            results = None
        return cls(
            no_wheel=no_wheel_record.read(
                state_dir / no_wheel_record.FILE_NAME,
                f"{sys.version_info.major}.{sys.version_info.minor}",
                wheel_architecture(),
            ),
            not_installed=frozenset(
                str(result["key"])
                for result in (results if isinstance(results, list) else [])
                if isinstance(result, dict)
                and "key" in result
                and result.get("status") != "ok"
            )
            | _interrupted(state_dir, last_install),
        )


def _interrupted(state_dir: Path, last_install: Any) -> frozenset[str]:
    """The keys of an install cut off before it wrote last_install.json, as
    by a power cut: install_pending keeps such a request as
    ``.in_progress.<ts>``, so a start must not queue it again by itself."""
    started_at = (
        last_install.get("started_at") if isinstance(last_install, dict) else None
    )
    recorded_up_to = int(started_at) if isinstance(started_at, (int, float)) else -1
    keys: set[str] = set()
    pattern = f"{Path(PENDING_INSTALLS_FILE).stem}.in_progress.*"
    for path in state_dir.glob(pattern):
        try:
            started = int(path.suffix[1:])
            requested = json.loads(path.read_text()).get("requested")
        except (OSError, ValueError, AttributeError):
            continue
        if started > recorded_up_to and isinstance(requested, list):
            keys.update(str(key) for key in requested)
    return frozenset(keys)


def _names(keys: Sequence[str]) -> str:
    return ", ".join(f"`{key}`" for key in keys)


@dataclass(frozen=True)
class Installability:
    """Which optional packages an install at the next start can bring, as
    the loaded plugins offer them and earlier installs found."""

    #: The keys a request may name: offered, with wheels for this box.
    installable: frozenset[str]
    #: {key: why} for each offered key no install can bring here.
    unavailable: Mapping[str, str]
    #: The keys the last install was asked for and did not install.
    failed: frozenset[str]

    @classmethod
    def of(
        cls,
        registry: dict[str, tuple[str, OptionalPackageSpec]],
        history: InstallHistory,
    ) -> Installability:
        architecture = wheel_architecture()
        unavailable = {
            key: f"No prebuilt {key} for {architecture}"
            for key, (_plugin_id, spec) in registry.items()
            if not spec.has_wheels_for(architecture)
        }
        unavailable.update(
            {
                key: entry["reason"]
                for key, entry in history.no_wheel.items()
                if key in registry and registry[key][1].pip_spec == entry["pip_spec"]
            }
        )
        return cls(
            installable=frozenset(_installable_here(registry)),
            unavailable=unavailable,
            failed=history.not_installed,
        )

    def blocking(self, keys: Sequence[str]) -> list[str]:
        """The keys among ``keys`` no install can bring here."""
        return [
            key
            for key in keys
            if key not in self.installable or key in self.unavailable
        ]

    def as_offered(self, state: ModuleState) -> ModuleState:
        """``state`` as the modules page shows it, saying what a restart does
        for its missing packages. When one of them cannot be installed here
        none is offered, as none is queued, and the message says why."""
        missing = list(dict.fromkeys(state.missing_packages))
        if not missing:
            return state
        blocked = self.blocking(missing)
        if blocked:
            offered, notes = [], [self._why_not(key) for key in blocked]
        else:
            offered, notes = missing, [self._what_a_restart_does(missing)]
        return state.model_copy(
            update={
                "missing_packages": offered,
                "message": "\n\n".join(
                    [state.message, *notes] if state.message else notes
                ),
            }
        )

    def _why_not(self, key: str) -> str:
        reason = self.unavailable.get(key)
        if reason is None:
            return f"`{key}` cannot be installed: no plugin offers it."
        return f"`{key}` is not available on this device. {reason}."

    def _what_a_restart_does(self, missing: list[str]) -> str:
        failed = [key for key in missing if key in self.failed]
        if failed:
            # A start holds a failed install back; Restart Kalinka asks again.
            return (
                f"Installing {_names(failed)} failed when Kalinka last started. "
                "Use **Restart Kalinka** to try again."
            )
        verb = "installs" if len(missing) == 1 else "install"
        return (
            f"Restart to finish setting up: {_names(missing)} {verb} as "
            "Kalinka starts."
        )


def build_catalog(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
    last_install_path: Path | None = None,
    pending_installs_path: Path | None = None,
) -> dict[str, Any]:
    """Return the catalog payload for GET /server/optional_packages.

    A package two plugins share is listed once, on the terms of the first;
    ``offered_by`` names every plugin that offers it and what for. One with
    no wheel for this box is left out, as no install could bring it.
    """
    pending_installs_path = pending_installs_path or _state_file(
        PENDING_INSTALLS_FILE
    )
    offers = _collect_offers(input_modules, devices)
    registry = _installable_here(_first_offers(offers))

    pending_keys: set[str] = set()
    if pending_installs_path.is_file():
        try:
            pending = json.loads(pending_installs_path.read_text())
            requested = pending.get("requested") or []
            if isinstance(requested, list):
                pending_keys = {str(k) for k in requested}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "Cannot read pending installs at %s: %s",
                pending_installs_path,
                e,
            )

    last_install = _read_last_install(
        last_install_path or _state_file(LAST_INSTALL_FILE)
    )

    packages: list[dict[str, Any]] = []
    for key, (declaring_plugin_id, spec) in sorted(registry.items()):
        packages.append(
            {
                "key": key,
                "plugin_id": declaring_plugin_id,
                "pip_spec": spec.pip_spec,
                "description": spec.description,
                "import_name": spec.import_name or key,
                "installed": _is_installed(spec, key),
                "pending": key in pending_keys,
                "offered_by": [
                    {"plugin_id": plugin_id, "description": offer.description}
                    for plugin_id, offer in offers[key]
                ],
            }
        )

    return {
        "schema_version": PENDING_SCHEMA_VERSION,
        "packages": packages,
        "last_install": last_install,
    }


def _write_pending(keys: list[str], pending_installs_path: Path | None) -> None:
    """Replace the request atomically (tmp + rename), so a reader sees the
    previous one or the new one, never a half-written blob. With no keys
    the file is removed: install_pending would replace last_install.json
    with a record of installing nothing."""
    pending_installs_path = pending_installs_path or _state_file(
        PENDING_INSTALLS_FILE
    )
    if not keys:
        pending_installs_path.unlink(missing_ok=True)
        return

    payload = {
        "schema": PENDING_SCHEMA_VERSION,
        "requested_at": time.time(),
        "requested": keys,
    }
    pending_installs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_installs_path.with_suffix(
        pending_installs_path.suffix + ".tmp"
    )
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.replace(tmp_path, pending_installs_path)


async def _required_by_plugin(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
) -> list[tuple[str, list[str]]]:
    """Each enabled plugin with the keys its ``required_packages()`` names
    now, in load order.

    A plugin that raises or does not answer in time is skipped, so neither a
    settings save nor a restart can fail on one.
    """
    asked = [
        (plugin_id, prepared.plugin_instance)
        for _kind, plugin_id, prepared in _iter_plugin_entries(input_modules, devices)
        if prepared.plugin_instance is not None
        and prepared.plugin_context.config.enabled
    ]
    answers = await asyncio.gather(
        *(_required_by(plugin_id, instance) for plugin_id, instance in asked)
    )
    return [
        (plugin_id, keys)
        for (plugin_id, _instance), keys in zip(asked, answers)
        if keys is not None
    ]


async def _required_by(plugin_id: str, instance: Any) -> list[str] | None:
    try:
        named = await asyncio.wait_for(
            instance.required_packages(), PLUGIN_CALL_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001 — defensive
        # The type only: a plugin's exception text may carry a credential.
        logger.warning(
            "Plugin %s required_packages() failed (%s); ignoring",
            plugin_id,
            type(e).__name__,
        )
        return None
    return list(dict.fromkeys(str(key) for key in named or []))


async def queue_required_packages(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
    installability: Installability,
    requested: Sequence[str] = (),
    hold_failed: bool = False,
    pending_installs_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Queue the keys asked for, then those the plugins need now, for the
    next start to install, whatever brings it.

    Returns (accepted, rejected). A key asked for that is unknown or has no
    wheel declared for this box refuses the request: nothing is written,
    ``rejected`` names those keys and ``accepted`` the rest. A failed install
    may be asked for again.

    Each plugin's keys are queued together or not at all, as a feature needs
    them all: they are left out, with a log line, when one cannot be installed
    here, or with ``hold_failed`` when the last install did not install one,
    so a start never retries a failure by itself. The queue is replaced whole,
    so a feature turned off since takes its keys back out.
    """
    asked = list(dict.fromkeys(str(key) for key in requested))
    rejected = [key for key in asked if key not in installability.installable]
    if rejected:
        return [key for key in asked if key not in rejected], rejected

    queued = list(asked)
    for plugin_id, keys in await _required_by_plugin(input_modules, devices):
        blocked = installability.blocking(keys)
        held = [key for key in keys if hold_failed and key in installability.failed]
        if blocked:
            logger.warning(
                "Not queueing %s for %s: %s cannot be installed on Python "
                "%d.%d on %s",
                keys,
                plugin_id,
                blocked,
                sys.version_info.major,
                sys.version_info.minor,
                wheel_architecture(),
            )
        elif held:
            logger.info(
                "Not queueing %s for %s: the last install failed %s",
                keys,
                plugin_id,
                held,
            )
        else:
            queued += [key for key in keys if key not in queued]
    _write_pending(queued, pending_installs_path)
    if queued:
        logger.info("Queued optional packages for the next start: %s", queued)
    return queued, []
