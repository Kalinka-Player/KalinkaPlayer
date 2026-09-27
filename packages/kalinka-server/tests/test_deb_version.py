"""The Debian version the Python package debs carry, and how dpkg orders it.

A dev deb was once versioned 5.1.2.dev1, which dpkg sorts above the 5.1.2
release it led up to, so installing that release read as a downgrade.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[3] / "scripts" / "deb_version.sh"


def _deb_version(pep440: str) -> str:
    return subprocess.run(
        ["bash", "-c", f'. "{HELPER}" && deb_version "$1"', "_", pep440],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    "pep440, deb",
    [
        ("5.1.2", "5.1.2"),
        ("5.1.2.dev1+gcb9a628", "5.1.2~dev1+gcb9a628"),
        ("5.0.1.dev30+gd6dd46f.d20260922", "5.0.1~dev30+gd6dd46f.d20260922"),
        ("3.3.0", "3.3.0"),
    ],
)
def test_a_dev_build_takes_a_tilde(pep440, deb):
    assert _deb_version(pep440) == deb


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="needs dpkg")
@pytest.mark.parametrize(
    "older, newer",
    [
        ("5.1.1", "5.1.2.dev1+gcb9a628"),
        ("5.1.2.dev1+gcb9a628", "5.1.2.dev2+g1234567"),
        ("5.1.2.dev1+gcb9a628", "5.1.2.dev1+gcb9a628.d20260926"),
        ("5.1.2.dev1+gcb9a628.d20260926", "5.1.2"),
    ],
)
def test_dpkg_orders_builds_as_pep_440_does(older, newer):
    assert (
        subprocess.run(
            [
                "dpkg",
                "--compare-versions",
                _deb_version(older),
                "lt",
                _deb_version(newer),
            ]
        ).returncode
        == 0
    )
