"""Metadata-only native-package preflight; never an installation authorization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib import metadata
import platform
import subprocess
import sys
from typing import Callable

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from ..version import get_version

NATIVE_ARCHITECTURES = {
    "deb": {
        "all": "all",
        "amd64": "x86_64",
        "arm64": "aarch64",
        "armhf": "armv7l",
        "i386": "x86",
        "riscv64": "riscv64",
    },
    "rpm": {
        "noarch": "all",
        "x86_64": "x86_64",
        "aarch64": "aarch64",
        "armv7hl": "armv7l",
        "i686": "x86",
        "riscv64": "riscv64",
    },
}
NATIVE_DISTROS = {
    "deb": {"debian", "ubuntu", "raspbian"},
    "rpm": {"fedora", "rhel", "rocky", "almalinux", "opensuse-leap"},
}


@dataclass(frozen=True)
class HostEnvironment:
    """Server userspace facts, independent of the phone or kernel architecture."""

    platform: str | None
    architecture: str | None
    package_format: str | None
    distribution: str | None
    distribution_version: str | None
    server_version: str | None
    sdk_version: str | None
    python_version: str | None
    capabilities: frozenset[str] = frozenset()

    def public(self) -> dict:
        result = asdict(self)
        result["capabilities"] = sorted(self.capabilities)
        return result


def _native_architecture(package_format: str) -> str | None:
    command = {
        "deb": ["/usr/bin/dpkg", "--print-architecture"],
        # Query installed bytes' owner, not a CPU/build-target macro.
        "rpm": ["/usr/bin/rpm", "-q", "--queryformat", "%{ARCH}\n", "rpm"],
    }[package_format]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    architecture = NATIVE_ARCHITECTURES[package_format].get(result.stdout.strip())
    if architecture not in {"x86_64", "aarch64", "armv7l", "x86", "riscv64"}:
        return None
    is_64_bit = architecture in {"x86_64", "aarch64", "riscv64"}
    return architecture if is_64_bit == (sys.maxsize > 2**32) else None


@lru_cache(maxsize=1)
def detect_host() -> HostEnvironment:
    """Read local package facts once per process; no network or plugin imports."""
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
        platform.system()
    )
    distribution = distribution_version = package_format = architecture = None
    if system == "linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        distribution = release.get("ID") or None
        distribution_version = release.get("VERSION_ID") or None
        package_format = next(
            (kind for kind, ids in NATIVE_DISTROS.items() if distribution in ids), None
        )
        if package_format:
            architecture = _native_architecture(package_format)
            if architecture is None:
                package_format = None
    try:
        sdk_version = metadata.version("kalinka-plugin-sdk")
    except metadata.PackageNotFoundError:
        sdk_version = None
    return HostEnvironment(
        system,
        architecture,
        package_format,
        distribution,
        distribution_version,
        get_version(),
        sdk_version,
        platform.python_version(),
    )


def _version_reasons(component: str, required: str, installed: str | None) -> list:
    try:
        version = Version(installed) if installed else None
    except InvalidVersion:
        version = None
    if version is None:
        return [{"code": f"{component}_version_unknown", "required": required}]
    if not SpecifierSet(required).contains(version, prereleases=True):
        return [
            {
                "code": f"incompatible_{component}",
                "required": required,
                "installed": str(version),
            }
        ]
    return []


def _target_reasons(kind: str, required: list, actual: str | None) -> list:
    if actual is None:
        return [{"code": f"{kind}_unknown", "required": required}]
    if "all" not in required and actual not in required:
        return [{"code": f"unsupported_{kind}", "required": required, "actual": actual}]
    return []


def _packaged_release(kind: str, native: str) -> Version | None:
    """Release in an ``[epoch:]version[-revision]``; RPM requires the revision."""
    epoch, _, version = native.rpartition(":")
    if epoch and not epoch.isdigit():
        return None
    if "-" in version:
        version, revision = version.rsplit("-", 1)
        if not revision:
            return None
    elif kind == "rpm":
        return None
    try:
        # Native packages spell a PEP 440 pre-release with a tilde: 1.0~rc1.
        return Version(version.replace("~", ""))
    except InvalidVersion:
        return None


def _artifact_reasons(
    plugin: dict, release_version: Version, artifact: dict, host: HostEnvironment
) -> list:
    kind = artifact["format"]
    if kind not in NATIVE_ARCHITECTURES:
        return [{"code": "unsupported_package_format", "format": kind}]
    package = artifact["package"]
    declared_arch = NATIVE_ARCHITECTURES[kind].get(package["architecture"])
    version = package["version"].split(":", 1)[-1]
    filename = (
        f"{package['name']}_{version}_{package['architecture']}.deb"
        if kind == "deb"
        else f"{package['name']}-{version}.{package['architecture']}.rpm"
    )
    if (
        declared_arch is None
        or artifact["architectures"] != [declared_arch]
        or package["name"] != plugin["distribution"]
        or _packaged_release(kind, package["version"]) != release_version
        or artifact["filename"] != filename
        or any(
            target["id"] not in NATIVE_DISTROS[kind] for target in artifact["targets"]
        )
    ):
        return [{"code": "invalid_package_metadata"}]
    reasons = _target_reasons("platform", [artifact["platform"]], host.platform)
    reasons += _target_reasons(
        "architecture", artifact["architectures"], host.architecture
    )
    if kind != host.package_format:
        reasons.append({"code": "unsupported_package_format", "format": kind})
    if not host.distribution or not host.distribution_version:
        reasons.append({"code": "distribution_unknown"})
    elif not any(
        target["id"] == host.distribution
        and host.distribution_version in target["versions"]
        for target in artifact["targets"]
    ):
        reasons.append(
            {
                "code": "unsupported_distribution",
                "id": host.distribution,
                "version": host.distribution_version,
            }
        )
    return reasons


def evaluate_release(
    plugin: dict,
    release: dict,
    host: HostEnvironment,
    renderers: tuple[dict, ...],
    *,
    channel: str,
    now: datetime,
) -> dict:
    """Evaluate schema-validated declarations without fetching package bytes."""
    requires = release["requires"]
    reasons = []
    if release["withdrawn"]:
        reasons.append({"code": "release_withdrawn"})
    if release["channel"] != channel:
        reasons.append({"code": "channel_not_selected"})
    if datetime.fromisoformat(release["published_at"]) > now:
        reasons.append({"code": "release_not_yet_published"})
    for component in ("server", "sdk", "python"):
        reasons += _version_reasons(
            component, requires[component], getattr(host, f"{component}_version")
        )
    reasons += _target_reasons("platform", requires["platforms"], host.platform)
    reasons += _target_reasons(
        "architecture", requires["architectures"], host.architecture
    )
    for capability in requires["capabilities"]:
        if capability not in host.capabilities:
            reasons.append({"code": "missing_capability", "capability": capability})
    if "renderer" in requires:
        if not renderers:
            reasons.append(
                {"code": "renderer_version_unknown", "required": requires["renderer"]}
            )
        for renderer in renderers:
            problems = _version_reasons(
                "renderer", requires["renderer"], renderer.get("software_version")
            )
            if renderer.get("status") != "connected":
                problems.append({"code": "renderer_unavailable"})
            if renderer.get("compatible") is not True:
                problems.append({"code": "renderer_protocol_incompatible"})
            reasons += [
                problem | {"renderer_id": renderer["renderer_id"]}
                for problem in problems
            ]
    artifacts = []
    release_version = Version(release["version"])
    for artifact in release["artifacts"]:
        problems = _artifact_reasons(plugin, release_version, artifact, host)
        artifacts.append(
            {
                "filename": artifact["filename"],
                "sha256": artifact["sha256"],
                "status": "blocked" if problems else "metadata_compatible",
                "reasons": problems,
            }
        )
    if not any(item["status"] == "metadata_compatible" for item in artifacts):
        reasons.append({"code": "no_compatible_artifact"})
    return {
        "version": release["version"],
        "channel": release["channel"],
        "status": "blocked" if reasons else "metadata_compatible",
        "reasons": reasons,
        "artifacts": artifacts,
    }


class CatalogCompatibility:
    """Annotate a private browsing snapshot; identity and installer gates stay shut."""

    def __init__(
        self,
        host: Callable[[], HostEnvironment] = detect_host,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._host = host
        self._clock = clock

    def annotate(self, snapshot: dict, renderers: tuple[dict, ...] = ()) -> dict:
        host, now = self._host(), self._clock()
        snapshot["compatibility"] = {
            "status": "evaluated",
            "host": host.public(),
            "renderer_scope": "all_known_renderers",
            "dependency_check": "not_evaluated",
            "device_match": "not_evaluated",
            "authorization": "not_granted",
        }
        for plugin in snapshot["plugins"]:
            releases = (
                [
                    evaluate_release(
                        plugin, release, host, renderers, channel="stable", now=now
                    )
                    for release in sorted(
                        plugin["releases"],
                        key=lambda r: Version(r["version"]),
                        reverse=True,
                    )
                ]
                if plugin["delivery"] == "independent"
                else []
            )
            candidate = next(
                (r for r in releases if r["status"] == "metadata_compatible"), None
            )
            eligible = [
                r
                for r in releases
                if not any(
                    reason["code"]
                    in {
                        "release_withdrawn",
                        "channel_not_selected",
                        "release_not_yet_published",
                    }
                    for reason in r["reasons"]
                )
            ]
            latest = eligible[0] if eligible else None
            plugin["compatibility"] = {
                "status": (
                    "bundle_managed"
                    if plugin["delivery"] == "bundle"
                    else (
                        "no_releases"
                        if not releases
                        else "metadata_compatible" if candidate else "blocked"
                    )
                ),
                "channel": "stable",
                "latest_available_version": latest["version"] if latest else None,
                "latest_compatible_version": (
                    candidate["version"] if candidate else None
                ),
                "newer_blocked_release": (
                    latest if latest and latest["status"] == "blocked" else None
                ),
                "releases": releases,
                "installation_allowed": False,
            }
        return snapshot
