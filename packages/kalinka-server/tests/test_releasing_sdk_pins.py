"""The SDK pins RELEASING.md works its examples through.

The doc quoted the 1.x pins through two major bumps, so a maintainer following
it to the next bump would have been told to widen pins the tree no longer had.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RELEASING = REPO / "RELEASING.md"
SDK_VERSION = REPO / "packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/_version.py"
PIN = re.compile(r">=(\d+)(?:\.\d+)*,<\d+")


def _sdk_major() -> int:
    return int(re.search(r'^__version__ = "(\d+)\.', SDK_VERSION.read_text(), re.M)[1])


def test_releasing_quotes_the_current_sdk_major_as_the_consumer_pin():
    major = _sdk_major()
    assert f"kalinka-plugin-sdk>={major},<{major + 1}" in RELEASING.read_text()


def test_releasing_quotes_no_pin_on_an_older_sdk_major():
    major = _sdk_major()
    stale = [
        pin.group()
        for pin in PIN.finditer(RELEASING.read_text())
        if int(pin[1]) < major
    ]
    assert stale == []
