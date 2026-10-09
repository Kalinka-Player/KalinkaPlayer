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

At runtime the server exposes the catalog and accepts install requests
keyed by allow-list key (never by pip spec). Requested keys are validated
against the in-memory registry and written to a pending-installs file;
bootstrap re-validates against the manifest before invoking pip.

Which packages a plugin currently needs is reported via ``get_state()`` →
``ModuleState.missing_packages``; the UI takes those keys directly to
queue an install via ``PUT /server/restart {"install": [...]}``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
        "for every Python and architecture the plugin runs on. A package the "
        "server itself depends on takes the server's own requirement, so "
        "installing it never moves the server's copy.",
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
