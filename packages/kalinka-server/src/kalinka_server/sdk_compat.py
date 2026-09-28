"""Runtime guards against an incompatible kalinka-plugin-sdk.

The server's own compatibility is declared in exactly one place — its
``kalinka-plugin-sdk`` dependency in ``pyproject.toml``. We read that
requirement back from installed package metadata and check the installed SDK
against it at startup, so there is no second copy of the supported range to
keep in sync.

Each plugin declares the SDK it was written for in ``REQUIRES_SDK``, which is
checked as the plugin is loaded. Both are the runtime backstop for the cases
dependency resolution can't catch (``pip install --no-deps``,
``dpkg --force-depends``, an in-place SDK upgrade to a different major, a
plugin an earlier boot installed whose wheel pip now refuses).

See RELEASING.md for the versioning policy.
"""

import logging
from importlib.metadata import PackageNotFoundError, requires
from importlib.metadata import version as dist_version

from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet

logger = logging.getLogger(__name__.split(".")[-1])

SERVER_DIST = "kalinka-server"
SDK_DIST = "kalinka-plugin-sdk"


class IncompatibleSDKError(RuntimeError):
    """Raised when the installed SDK does not satisfy the server's requirement."""


def _server_sdk_requirement() -> Requirement | None:
    """Return the server's declared ``kalinka-plugin-sdk`` requirement, if any."""
    try:
        declared = requires(SERVER_DIST) or []
    except PackageNotFoundError:
        # Running from a source tree that was never installed as a dist.
        return None
    for raw in declared:
        req = Requirement(raw)
        # Skip requirements gated by a marker (e.g. `; extra == "dev"`); only the
        # unconditional runtime requirement is the one we enforce.
        if req.name == SDK_DIST and not req.marker:
            return req
    return None


def check_sdk_compatibility() -> None:
    """Raise :class:`IncompatibleSDKError` if the installed SDK is incompatible.

    No-ops with a warning when the requirement or the SDK version can't be
    determined (e.g. an uninstalled source checkout), so development isn't
    blocked.
    """
    req = _server_sdk_requirement()
    if req is None:
        logger.warning(
            "Could not determine the required %s version; skipping SDK compatibility check",
            SDK_DIST,
        )
        return

    try:
        installed = dist_version(SDK_DIST)
    except PackageNotFoundError as exc:
        raise IncompatibleSDKError(
            f"{SDK_DIST} is required ({req.specifier}) but is not installed"
        ) from exc

    # prereleases=True so local dev builds (e.g. 1.0.1.dev5+...) are accepted.
    if not req.specifier.contains(installed, prereleases=True):
        raise IncompatibleSDKError(
            f"Incompatible {SDK_DIST} {installed}: this server requires "
            f"{SDK_DIST}{req.specifier}. Install a matching SDK "
            f"(and rebuild plugins for this major)."
        )

    logger.info(
        "SDK compatibility OK: %s %s satisfies %s", SDK_DIST, installed, req.specifier
    )


def plugin_sdk_mismatch(plugin_class: type, sdk_version: str) -> str | None:
    """Why a plugin must not run on ``sdk_version``, or None when it may.

    Judged by the plugin's ``REQUIRES_SDK`` specifier. One that is missing,
    empty or does not parse counts against the plugin: it cannot say which SDK
    it fits.

    @return A reason fit to show the user beside the plugin.
    """
    declared = getattr(plugin_class, "REQUIRES_SDK", None)
    if not isinstance(declared, str) or not declared.strip():
        return "Declares no REQUIRES_SDK, so the SDK it was written for is unknown"
    try:
        specifier = SpecifierSet(declared)
    except InvalidSpecifier:
        return f"REQUIRES_SDK {declared!r} is not a version specifier"
    # prereleases=True so local dev builds (e.g. 3.5.0.dev2+...) are accepted.
    if specifier.contains(sdk_version, prereleases=True):
        return None
    return (
        f"Needs {SDK_DIST}{declared}, but {sdk_version} is installed. "
        f"Install a release of the plugin built for this SDK."
    )
