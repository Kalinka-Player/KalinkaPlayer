"""Runtime registry for plugin-declared optional packages.

Each plugin class may declare `OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec]`
as a class attribute. This module collects them from the currently loaded
plugins (input modules + devices), probes whether each is importable, and
serves the catalog used by the settings UI.

Install requests go through this module: keys are validated against the
in-memory registry and written to /var/lib/kalinka/pending_installs.json.
Bootstrap re-validates against the deb-shipped manifest at
/opt/kalinka/allowed_packages/<plugin>.json before invoking pip — i.e.
this in-process check is convenience, not the security boundary.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kalinka_plugin_sdk import OptionalPackageSpec, wheel_architecture
from kalinka_plugin_sdk import paths

from .player_setup import PreparedPlugin


logger = logging.getLogger(__name__.split(".")[-1])


# Default state file locations (under $KALINKA_PREFIX, default /var/lib/kalinka).
# Kept overridable for tests.
PENDING_INSTALLS_PATH = Path(paths.state_dir()) / "pending_installs.json"
LAST_INSTALL_PATH = Path(paths.state_dir()) / "last_install.json"
PENDING_SCHEMA_VERSION = 1


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


def build_catalog(
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
    last_install_path: Path = LAST_INSTALL_PATH,
    pending_installs_path: Path = PENDING_INSTALLS_PATH,
) -> dict[str, Any]:
    """Return the catalog payload for GET /server/optional_packages.

    A package two plugins share is listed once, on the terms of the first;
    ``offered_by`` names every plugin that offers it and what for. One with
    no wheel for this box is left out, as no install could bring it.
    """
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

    last_install: dict[str, Any] | None = None
    if last_install_path.is_file():
        try:
            last_install = json.loads(last_install_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "Cannot read last install at %s: %s", last_install_path, e
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


def write_pending_installs(
    keys: list[str],
    input_modules: dict[str, PreparedPlugin],
    devices: dict[str, PreparedPlugin],
    pending_installs_path: Path = PENDING_INSTALLS_PATH,
) -> tuple[list[str], list[str]]:
    """Validate and persist a pending-installs request.

    Returns (accepted, rejected). Caller decides what to do with each. A key
    with no wheel for this box is rejected like an unknown one: the catalog
    leaves it out too.

    Writes atomically (tmp + rename) so a reader of the file either sees
    the previous request or the new one, never a half-written blob.
    """
    registry = _installable_here(build_registry(input_modules, devices))
    accepted: list[str] = []
    rejected: list[str] = []
    for raw in keys:
        key = str(raw)
        if key in registry:
            if key not in accepted:
                accepted.append(key)
        else:
            rejected.append(key)

    payload = {
        "schema": PENDING_SCHEMA_VERSION,
        "requested_at": time.time(),
        "requested": accepted,
    }
    pending_installs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_installs_path.with_suffix(
        pending_installs_path.suffix + ".tmp"
    )
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.replace(tmp_path, pending_installs_path)

    return accepted, rejected
