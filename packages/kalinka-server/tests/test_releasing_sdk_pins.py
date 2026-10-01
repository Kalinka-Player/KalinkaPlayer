"""The SDK pins RELEASING.md works its examples through.

The doc quoted the 1.x pins through two major bumps, so a maintainer following
it to the next bump would have been told to widen pins the tree no longer had.
Bare versions are checked only in SDK prose; other release lines are independent.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RELEASING = REPO / "RELEASING.md"
SDK_VERSION = REPO / "packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py"
SERVER_PYPROJECT = REPO / "packages/kalinka-server/pyproject.toml"
PIN = re.compile(r"`(?:kalinka-plugin-sdk)?>=(\d+)(?:\.\d+)*,<(\d+)`")
UPPER_BOUND = re.compile(r"`<(\d+)`")
SERIES = re.compile(r"`(\d+)\.x`")
MINOR_STEP = re.compile(r"`(\d+)\.\d+(?:\.\d+)?`? → `?(\d+)\.\d+(?:\.\d+)?")
SDK_DEB = re.compile(r"kalinka-plugin-sdk_(\d+)\.")
SDK_SECTIONS = (
    r"^- The \*\*SDK\*\*.*?(?=\n\S|\Z)",
    r"^## Bump the SDK version\n.*?(?=^## |\Z)",
    r"^- \*\*SDK = one constant\.\*\*.*?(?=\n\S|\Z)",
)


def _sdk_major() -> int:
    return int(re.search(r'^__version__ = "(\d+)\.', SDK_VERSION.read_text(), re.M)[1])


def _server_sdk_pin() -> str:
    return re.search(r'"kalinka-plugin-sdk(>=[^"]+)"', SERVER_PYPROJECT.read_text())[1]


def _sdk_sections() -> list[str]:
    text = RELEASING.read_text()
    sections = []
    for pattern in SDK_SECTIONS:
        match = re.search(pattern, text, re.M | re.S)
        assert match, f"Missing SDK prose matching {pattern}"
        sections.append(match[0])
    return sections


def test_releasing_quotes_the_current_sdk_major_as_the_consumer_pin():
    major = _sdk_major()
    assert f"kalinka-plugin-sdk>={major},<{major + 1}" in RELEASING.read_text()


def test_releasing_works_the_major_bump_through_to_the_next_major():
    major = _sdk_major()
    overview, bump, _ = _sdk_sections()
    assert f"`→ {major + 1}.0`" in overview
    assert f"`{major}.x` → `{major + 1}.0.0`" in bump
    assert f"in `_version.py` to `{major + 1}.0.0`" in bump
    assert f"kalinka-plugin-sdk>={major + 1},<{major + 2}" in bump


def test_releasing_quotes_no_pin_on_an_older_sdk_major():
    major = _sdk_major()
    stale = [
        pin.group()
        for pin in PIN.finditer("\n".join(_sdk_sections()))
        if int(pin[1]) < major or int(pin[2]) <= major
    ]
    assert stale == []


def test_releasing_quotes_no_bare_bound_or_series_on_an_older_sdk_major():
    major = _sdk_major()
    text = "\n".join(_sdk_sections())
    stale = [m.group() for m in UPPER_BOUND.finditer(text) if int(m[1]) <= major]
    stale += [m.group() for m in SERIES.finditer(text) if int(m[1]) < major]
    assert stale == []


def test_releasing_works_minor_bumps_and_the_deb_name_on_the_current_sdk_major():
    major = _sdk_major()
    steps = MINOR_STEP.findall("\n".join(_sdk_sections()))
    deb_majors = SDK_DEB.findall(RELEASING.read_text())
    assert steps and deb_majors, "Missing SDK minor-bump or deb examples"
    assert all(int(start) == int(end) == major for start, end in steps)
    assert all(int(deb_major) == major for deb_major in deb_majors)


def test_releasing_quotes_the_servers_own_sdk_pin():
    overview, bump, _ = _sdk_sections()
    for section in (overview, bump):
        assert f"`{_server_sdk_pin()}`" in section
