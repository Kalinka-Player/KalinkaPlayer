"""Every package a plugin offers to install has a wheel for each box, and
installing it leaves what the server installed alone.

install_pending installs them with --only-binary=:all:, so a pin with no
wheel for a box's Python and architecture fails there instead of compiling.
These resolve every plugin's optional pip_spec on PyPI for each Python and
architecture a box runs it on, and skip when PyPI cannot be reached. An
architecture a spec leaves out must really have no wheel, or the plugin
calls a feature unavailable that one install would bring.

bootstrap.sh installs the server and its plugins the same way, from piwheels
and PyPI, so their own dependencies are also resolved for 32-bit Raspberry Pi
OS, the one box that takes piwheels' wheels.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from kalinka_plugin_localfiles.optional_packages import (
    OPTIONAL_PACKAGES as LOCALFILES_OPTIONAL_PACKAGES,
)
from kalinka_plugin_sdk import OptionalPackageSpec
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SERVER_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
SDK_PYPROJECT = (
    Path(__file__).resolve().parents[2] / "kalinka-plugin-sdk" / "pyproject.toml"
)
PLUGIN_PYPROJECTS = tuple(
    path
    for path in sorted(SDK_PYPROJECT.parents[1].glob("kalinka-plugin-*/pyproject.toml"))
    if path != SDK_PYPROJECT
)
PYPI = "https://pypi.org/simple"
PIWHEELS = "https://www.piwheels.org/simple"
# Every Python the packages accept, each with the glibc of the oldest system
# that ships it: Debian 12's for 3.11, Ubuntu 24.04's for 3.12, Debian 13's
# (Raspberry Pi OS, DietPi, the images) for 3.13 and Fedora 43's for 3.14.
GLIBC_MINOR = {"3.11": 36, "3.12": 39, "3.13": 41, "3.14": 42}
ARCHITECTURES = ("aarch64", "x86_64")
# 32-bit Raspberry Pi OS on Debian 13, built for ARMv6 so it also runs on a
# Pi 1 or the first Zero.
ARM32_PYTHONS = ("3.13",)
ARM32_ARCHITECTURES = ("armv7l", "armv6l")


def optional_packages() -> list[tuple[str, OptionalPackageSpec]]:
    """Every (key, spec) a plugin offers, as the server registers them."""
    offers: list[tuple[str, OptionalPackageSpec]] = []
    for plugin in entry_points(group="kalinka.plugins"):
        offers += getattr(plugin.load(), "OPTIONAL_PACKAGES", {}).items()
    return offers


def optional_package_specs(arch: str | None = None) -> list[str]:
    """Every pip_spec offered, or offered on ``arch``."""
    return [
        spec.pip_spec
        for _key, spec in optional_packages()
        if arch is None or spec.has_wheels_for(arch)
    ]


def index_requirements(*pyprojects: Path) -> list[str]:
    """What installing these packages' wheels pulls from an index: their
    requirements and the SDK's with its media extra, which the server asks
    for, but no Kalinka package, since those ship beside them."""
    sdk = tomllib.loads(SDK_PYPROJECT.read_text())["project"]
    requirements = sdk["dependencies"] + sdk["optional-dependencies"]["media"]
    for pyproject in pyprojects:
        requirements += tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    return [
        requirement
        for requirement in requirements
        if not canonicalize_name(Requirement(requirement).name).startswith("kalinka-")
    ]


def server_requirements() -> list[str]:
    return index_requirements(SERVER_PYPROJECT)


def wheel_platforms(arch: str, glibc_minor: int) -> list[str]:
    """Every manylinux tag a glibc this new runs, since pip given one tag
    matches only that tag, and the plain tag piwheels builds with."""
    legacy = ("manylinux1", "manylinux2010", "manylinux2014")
    return (
        [f"{tag}_{arch}" for tag in legacy]
        + [f"manylinux_2_{minor}_{arch}" for minor in range(5, glibc_minor + 1)]
        + [f"linux_{arch}"]
    )


def resolve(
    specs: list[str],
    python: str,
    arch: str,
    *pip_args: str,
    indexes: tuple[str, ...] = (PYPI,),
) -> subprocess.CompletedProcess:
    platforms = wheel_platforms(arch, GLIBC_MINOR[python])
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--only-binary=:all:",
            "--isolated",
            "--no-input",
            "--quiet",
            "--index-url",
            indexes[0],
            *(arg for index in indexes[1:] for arg in ("--extra-index-url", index)),
            "--implementation",
            "cp",
            "--python-version",
            python,
            "--abi",
            "cp" + python.replace(".", ""),
            *(arg for platform in platforms for arg in ("--platform", platform)),
            *pip_args,
            *specs,
        ],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def pypi():
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
    except OSError as e:
        pytest.skip(f"PyPI cannot be reached: {e}")


@pytest.fixture(scope="module")
def piwheels(pypi):
    try:
        socket.create_connection(("www.piwheels.org", 443), timeout=5).close()
    except OSError as e:
        pytest.skip(f"piwheels cannot be reached: {e}")


def test_every_plugins_optional_packages_are_checked():
    specs = optional_package_specs()

    assert {
        spec.pip_spec for spec in LOCALFILES_OPTIONAL_PACKAGES.values()
    } <= set(specs)


def test_a_package_two_plugins_offer_is_offered_on_the_same_terms():
    """The registry and install_pending keep the first plugin's spec for a
    shared key, so a second spec would never be what gets installed."""
    terms: dict[str, set[tuple]] = {}
    for key, spec in optional_packages():
        terms.setdefault(key, set()).add(
            (spec.pip_spec, spec.import_name or key, spec.architectures)
        )

    assert {key: found for key, found in terms.items() if len(found) > 1} == {}


def test_a_package_the_server_requires_is_offered_on_its_terms():
    """The server installs its own dependencies unpinned; a pin on an
    optional package would move that copy when someone asks for it."""
    project = tomllib.loads(SERVER_PYPROJECT.read_text())["project"]
    server = {
        canonicalize_name(requirement.name): requirement.specifier
        for requirement in map(Requirement, project["dependencies"])
    }

    for offered in map(Requirement, optional_package_specs()):
        name = canonicalize_name(offered.name)
        if name in server:
            assert offered.specifier == server[name], offered


def test_pip_takes_the_check_as_written():
    """Offline, with no index: pip gets as far as looking the package up, so
    a run against PyPI fails only on what PyPI lacks."""
    result = resolve(["numpy==2.0.0"], "3.13", "aarch64", "--no-index")

    assert "No matching distribution found for numpy==2.0.0" in result.stderr


@pytest.mark.parametrize("arch", ARCHITECTURES)
@pytest.mark.parametrize("python", GLIBC_MINOR)
def test_every_optional_package_installs_from_wheels(pypi, python, arch):
    """One resolution, so the pins also have to agree with each other."""
    result = resolve(optional_package_specs(arch), python, arch)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("arch", ARM32_ARCHITECTURES)
@pytest.mark.parametrize("python", ARM32_PYTHONS)
def test_every_package_offered_on_32_bit_arm_installs_there(piwheels, python, arch):
    result = resolve(
        optional_package_specs(arch), python, arch, indexes=(PIWHEELS, PYPI)
    )

    assert result.returncode == 0, result.stderr


def test_an_architecture_a_package_leaves_out_has_no_wheel_for_it(piwheels):
    """There a plugin calls the feature unavailable; once a wheel appears the
    package should be offered instead."""
    installable_anyway = [
        (key, arch)
        for key, spec in dict(optional_packages()).items()
        for arch in (*ARCHITECTURES, *ARM32_ARCHITECTURES)
        if not spec.has_wheels_for(arch)
        and resolve(
            [spec.pip_spec], "3.13", arch, indexes=(PIWHEELS, PYPI)
        ).returncode
        == 0
    ]

    assert installable_anyway == []


def test_a_pin_without_a_wheel_fails_the_check(pypi):
    """numpy 1.26.4 has no wheel for Python 3.13: every Pi that turned Smart
    Search on built it from source."""
    result = resolve(["numpy==1.26.4"], "3.13", "aarch64")

    assert result.returncode != 0
    assert "No matching distribution found for numpy==1.26.4" in result.stderr


@pytest.mark.parametrize("arch", ARM32_ARCHITECTURES)
@pytest.mark.parametrize("python", ARM32_PYTHONS)
def test_the_server_installs_from_wheels_on_32_bit_arm(piwheels, python, arch):
    """A dependency with no 32-bit ARM wheel fails bootstrap's joint install,
    and its one-at-a-time retry then skips the server's own wheel: the Pi is
    left with no server."""
    result = resolve(server_requirements(), python, arch, indexes=(PIWHEELS, PYPI))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("arch", ARM32_ARCHITECTURES)
@pytest.mark.parametrize("python", ARM32_PYTHONS)
def test_every_plugin_installs_from_wheels_on_32_bit_arm(piwheels, python, arch):
    """One resolution, as bootstrap installs the bundle: a plugin dependency
    with no wheel there once left 32-bit boxes without My Library."""
    result = resolve(
        index_requirements(SERVER_PYPROJECT, *PLUGIN_PYPROJECTS),
        python,
        arch,
        indexes=(PIWHEELS, PYPI),
    )

    assert result.returncode == 0, result.stderr


def test_a_dependency_without_an_armv7l_wheel_fails_the_check(piwheels):
    """onnxruntime publishes no 32-bit ARM wheel and piwheels builds none,
    which is why it is not a server dependency."""
    result = resolve(["onnxruntime>=1.17"], "3.13", "armv7l", indexes=(PIWHEELS, PYPI))

    assert result.returncode != 0
    assert "No matching distribution found for onnxruntime" in result.stderr
