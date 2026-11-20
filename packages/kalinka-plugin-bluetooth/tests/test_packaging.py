from configparser import ConfigParser
from pathlib import Path

UNIT = Path(__file__).parents[1] / "packaging" / "kalinka-bluealsa.service"


def test_receiver_unit_leaves_the_bluealsa_bus_name_to_the_distribution_unit():
    unit = ConfigParser(strict=False, interpolation=None)
    unit.read(UNIT)
    assert "BusName" not in unit["Service"]
    assert unit["Service"]["Type"] != "dbus"
