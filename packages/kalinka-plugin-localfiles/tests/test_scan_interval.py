"""The library's scan interval: 0 leaves rescans to the user, and no interval
is below it."""

import pytest
from kalinka_server.config_overrides import apply_overrides_with_prefix
from pydantic import ValidationError

from kalinka_plugin_localfiles.config_model import LocalFilesConfig


def test_an_interval_of_0_is_accepted():
    assert LocalFilesConfig(scan_interval_minutes=0).scan_interval_minutes == 0


def test_a_negative_interval_is_refused():
    with pytest.raises(ValidationError):
        LocalFilesConfig(scan_interval_minutes=-1)


def test_a_saved_negative_interval_gives_way_to_the_default():
    """One saved while negatives were still accepted costs that setting only,
    not the rest of the library's."""
    config = LocalFilesConfig()

    apply_overrides_with_prefix(
        config,
        {
            "input_modules.localfiles.scan_interval_minutes": -1,
            "input_modules.localfiles.file_watch_enabled": False,
        },
        "input_modules.localfiles.",
    )

    assert config.scan_interval_minutes == 15
    assert config.file_watch_enabled is False
