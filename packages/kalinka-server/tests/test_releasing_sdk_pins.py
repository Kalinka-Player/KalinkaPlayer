"""The SDK pins RELEASING.md works its examples through.

The doc quoted the 1.x pins through two major bumps, so a maintainer following
it to the next bump would have been told to widen pins the tree no longer had.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RELEASING = REPO / "RELEASING.md"
SDK_VERSION = REPO / "packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py"
SERVER_PYPROJECT = REPO / "packages/kalinka-server/pyproject.toml"
PIN = re.compile(r">=(\d+)(?:\.\d+)*,<\d+")
UPPER_BOUND = re.compile(r"`<(\d+)`")
SERIES = re.compile(r"`(\d+)\.x`")
MINOR_STEP = re.compile(r"`(\d+)\.\d+(?:\.\d+)?`? →")
SDK_DEB = re.compile(r"kalinka-plugin-sdk_(\d+)\.")


def _sdk_major() -> int:
    return int(re.search(r'^__version__ = "(\d+)\.', SDK_VERSION.read_text(), re.M)[1])


def _server_sdk_pin() -> str:
    return re.search(r'"kalinka-plugin-sdk(>=[^"]+)"', SERVER_PYPROJECT.read_text())[1]


def test_releasing_quotes_the_current_sdk_major_as_the_consumer_pin():
    major = _sdk_major()
    assert f"kalinka-plugin-sdk>={major},<{major + 1}" in RELEASING.read_text()


def test_releasing_works_the_major_bump_through_to_the_next_major():
    # Left alone, the old example names the current major once the SDK moves up.
    major = _sdk_major()
    assert f"kalinka-plugin-sdk>={major + 1},<{major + 2}" in RELEASING.read_text()


def test_releasing_quotes_no_pin_on_an_older_sdk_major():
    major = _sdk_major()
    stale = [
        pin.group()
        for pin in PIN.finditer(RELEASING.read_text())
        if int(pin[1]) < major
    ]
    assert stale == []


def test_releasing_quotes_no_bare_bound_or_series_on_an_older_sdk_major():
    major = _sdk_major()
    text = RELEASING.read_text()
    stale = [m.group() for m in UPPER_BOUND.finditer(text) if int(m[1]) <= major]
    stale += [m.group() for m in SERIES.finditer(text) if int(m[1]) < major]
    assert stale == []


def test_releasing_works_minor_bumps_and_the_deb_name_on_the_current_sdk_major():
    major = _sdk_major()
    text = RELEASING.read_text()
    stale = [m.group() for m in MINOR_STEP.finditer(text) if int(m[1]) != major]
    stale += [m.group() for m in SDK_DEB.finditer(text) if int(m[1]) != major]
    assert stale == []


def test_releasing_quotes_the_servers_own_sdk_pin():
    assert f"`{_server_sdk_pin()}`" in RELEASING.read_text()
