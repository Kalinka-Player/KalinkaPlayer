"""A date tag yields a year only where a record could carry it."""

import pytest

from kalinka_plugin_localfiles.utils.years import year_from_date


@pytest.mark.parametrize(
    "date,expected",
    [
        ("1979", 1979),
        ("1979-11-30", 1979),
        ("20110101", 2011),
        ("0000", None),
        ("0000-00-00", None),
        ("unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_year_from_date(date, expected):
    assert year_from_date(date) == expected
