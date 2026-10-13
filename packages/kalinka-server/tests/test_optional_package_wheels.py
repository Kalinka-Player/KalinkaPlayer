"""Every package a plugin offers to install has a wheel for each box, and
installing it leaves what the server installed alone.

install_pending installs them with --only-binary=:all:, so a pin with no
wheel for a box's Python and architecture fails there instead of compiling.
These resolve every plugin's optional pip_spec on PyPI for each Python and
architecture a box runs, and skip when PyPI cannot be reached.
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
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SERVER_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
PYPI = "https://pypi.org/simple"
# Each Python with the glibc of the system that ships it: Debian 12, Ubuntu
# 24.04, Debian 13 under Raspberry Pi OS, DietPi and the images, and Fedora 43
# for the RPM.
GLIBC_MINOR = {"3.11": 36, "3.12": 39, "3.13": 41, "3.14": 42}
ARCHITECTURES = ("aarch64", "x86_64")


def optional_packages() -> list[tuple[str, str]]:
    """Every (key, pip_spec) a plugin offers, as the server registers them."""
    offers: list[tuple[str, str]] = []
    for plugin in entry_points(group="kalinka.plugins"):
        declared = getattr(plugin.load(), "OPTIONAL_PACKAGES", {})
        offers += [(key, spec.pip_spec) for key, spec in declared.items()]
    return offers


def optional_package_specs() -> list[str]:
    return [pip_spec for _key, pip_spec in optional_packages()]


def manylinux_platforms(arch: str, glibc_minor: int) -> list[str]:
    """Every manylinux tag a glibc this new runs, since pip given one tag
    matches only that tag."""
    legacy = ("manylinux1", "manylinux2010", "manylinux2014")
    return [f"{tag}_{arch}" for tag in legacy] + [
        f"manylinux_2_{minor}_{arch}" for minor in range(5, glibc_minor + 1)
    ]


def resolve(
    specs: list[str], python: str, arch: str, *pip_args: str
) -> subprocess.CompletedProcess:
    platforms = manylinux_platforms(arch, GLIBC_MINOR[python])
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
            PYPI,
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


def test_every_plugins_optional_packages_are_checked():
    specs = optional_package_specs()

    assert {
        spec.pip_spec for spec in LOCALFILES_OPTIONAL_PACKAGES.values()
    } <= set(specs)


def test_a_package_two_plugins_offer_is_offered_on_the_same_terms():
    """The registry and install_pending keep the first plugin's spec for a
    shared key, so a second spec would never be what gets installed."""
    specs: dict[str, set[str]] = {}
    for key, pip_spec in optional_packages():
        specs.setdefault(key, set()).add(pip_spec)

    assert {key: found for key, found in specs.items() if len(found) > 1} == {}


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
    result = resolve(optional_package_specs(), python, arch)

    assert result.returncode == 0, result.stderr


def test_a_pin_without_a_wheel_fails_the_check(pypi):
    """numpy 1.26.4 has no wheel for Python 3.13: every Pi that turned Smart
    Search on built it from source."""
    result = resolve(["numpy==1.26.4"], "3.13", "aarch64")

    assert result.returncode != 0
    assert "No matching distribution found for numpy==1.26.4" in result.stderr
