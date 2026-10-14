"""Declarations for optional pip packages a plugin may install on demand.

A plugin declares a static allow-list as a class attribute:

    class MyPlugin(InputModulePlugin):
        OPTIONAL_PACKAGES: ClassVar[dict[str, OptionalPackageSpec]] = {
            "soundfile": OptionalPackageSpec(
                pip_spec="soundfile==0.13.1",
                description="Used by AI search",
            ),
        }

At deb build time the plugin's allow-list is exported to a JSON manifest
shipped under /opt/kalinka/allowed_packages/<plugin_id>.json (root-owned).
The module declaring it prints the manifest when run as a script, which is
how the package builds call it:

    if __name__ == "__main__":
        print_optional_packages_manifest(PLUGIN_ID, OPTIONAL_PACKAGES)

At runtime the server exposes the catalog and accepts install requests
keyed by allow-list key (never by pip spec). Requested keys are validated
against the in-memory registry and written to a pending-installs file;
bootstrap re-validates against the manifest before invoking pip.

Which packages a plugin currently needs is reported via ``get_state()`` →
``ModuleState.missing_packages``; the UI takes those keys directly to
queue an install via ``PUT /server/restart {"install": [...]}``. A package
with no wheel for the box's architecture is never reported there: no
install could bring it, so the plugin says the feature is unavailable on
this device instead.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_SCHEMA_VERSION = 1


def wheel_architecture() -> str:
    """SDK 3.12: the architecture pip installs wheels for in this
    interpreter, as wheel tags name it (x86_64, aarch64, armv7l, i686).

    A 32-bit Python on a 64-bit kernel, as 32-bit Raspberry Pi OS runs on a
    Pi 4 or 5, takes armv7l or i686 wheels though the kernel reports aarch64
    or x86_64, or armv8l under a 32-bit personality.
    """
    machine = platform.machine()
    if sys.maxsize > 2**32:
        return machine
    return {"aarch64": "armv7l", "armv8l": "armv7l", "x86_64": "i686"}.get(
        machine, machine
    )


class OptionalPackageSpec(BaseModel):
    """Static declaration of an installable pip package.

    Pinned versions (or version ranges) live here, not in the install
    request — what gets installed is decided by the deb-shipped manifest,
    not by the API caller.
    """

    model_config = ConfigDict(frozen=True)

    pip_spec: str = Field(
        ...,
        description="Pip requirement specifier, e.g. 'soundfile==0.13.1'. It "
        "is installed from a wheel only, so it must name a release with one "
        "for every Python the plugin runs on and every architecture in "
        "`architectures`. A package the server itself depends on takes the "
        "server's own requirement, so installing it never moves the server's "
        "copy, and a key another plugin also offers takes that plugin's spec, "
        "since only one of them is installed.",
    )
    description: str = Field(
        ...,
        description="Human-readable description shown alongside the package "
        "in the settings UI.",
    )
    import_name: str = Field(
        default="",
        description="Python import name used to probe whether the package is "
        "already installed. Defaults to the registry key when empty. "
        "Set explicitly when the pip distribution name differs from the "
        "import name (e.g. 'essentia-tensorflow' -> 'essentia').",
    )
    architectures: tuple[str, ...] = Field(
        default=(),
        description="SDK 3.12: the architectures the package has wheels for, "
        "as wheel_architecture() names them; empty when it has them for "
        "every one the plugin runs on. A key another plugin also offers "
        "takes that plugin's list.",
    )

    def has_wheels_for(self, architecture: str) -> bool:
        """SDK 3.12: whether an install on ``architecture`` can succeed."""
        return not self.architectures or architecture in self.architectures


def optional_packages_manifest(
    plugin_id: str, packages: Mapping[str, OptionalPackageSpec]
) -> dict[str, Any]:
    """SDK 3.12: the manifest a plugin's package ships, naming every package
    the install helper may install for it, on the plugin's terms."""
    return {
        "plugin": plugin_id,
        "schema": MANIFEST_SCHEMA_VERSION,
        "packages": {
            key: spec.model_dump(mode="json") for key, spec in packages.items()
        },
    }


def print_optional_packages_manifest(
    plugin_id: str, packages: Mapping[str, OptionalPackageSpec]
) -> None:
    """SDK 3.12: print :func:`optional_packages_manifest` as JSON, as the
    package builds read it from the declaring module run as a script."""
    json.dump(optional_packages_manifest(plugin_id, packages), sys.stdout, indent=2)
    sys.stdout.write("\n")
