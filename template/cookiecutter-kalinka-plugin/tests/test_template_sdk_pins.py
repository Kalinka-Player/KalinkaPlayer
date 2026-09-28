"""The plugin template's SDK pins, against the SDK in this tree.

The template once pinned the wheel to SDK 2 while the SDK was at 3.4 and the
template's own deb already asked for 3, so a generated plugin would not
install next to the current SDK.
"""

import json
import re
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

import kalinka_plugin_sdk

TEMPLATE = Path(__file__).resolve().parents[1]
SDK = Version(kalinka_plugin_sdk.__version__)


def _wheel_pin() -> SpecifierSet:
    context = json.loads((TEMPLATE / "cookiecutter.json").read_text())
    return SpecifierSet(context["sdk_version_constraint"])


def _deb_pin_majors() -> tuple[int, int]:
    control = (
        TEMPLATE / "{{cookiecutter.plugin_name}}" / "debian" / "control.in"
    ).read_text()
    floor = re.search(r"kalinka-plugin-sdk \(>= (\d+)\.0\)", control)
    ceiling = re.search(r"kalinka-plugin-sdk \(<< (\d+)\)", control)
    assert floor and ceiling, "control.in no longer pins (>= N.0), (<< N+1)"
    return int(floor.group(1)), int(ceiling.group(1))


def test_the_wheel_pin_accepts_this_sdk():
    assert SDK in _wheel_pin()


def test_the_deb_pin_spans_this_sdk_major():
    assert _deb_pin_majors() == (SDK.major, SDK.major + 1)


def test_the_wheel_pin_spans_the_deb_pin_major():
    floor, ceiling = _deb_pin_majors()
    bounds = {spec.operator: Version(spec.version) for spec in _wheel_pin()}

    assert bounds.keys() == {">=", "<"}
    assert bounds[">="].major == floor
    assert bounds["<"] == Version(str(ceiling))
