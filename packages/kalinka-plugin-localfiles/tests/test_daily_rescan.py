"""The library rescans once a night, or only when asked. The scan interval
servers before this one kept stays in step with the switch: one an older
server saved carries over, an older client may still write it, and a
downgrade reads it back."""

from dataclasses import dataclass
from typing import Any

import pytest
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_validation import (
    ConfigTargets,
    commit_change,
    reconcile_written,
    stored_value,
)
from kalinka_server.player_setup import PreparedModuleCollection

from kalinka_plugin_localfiles.config_model import (
    LEGACY_DAILY_INTERVAL_MINUTES,
    LocalFilesConfig,
)
from kalinka_plugin_localfiles.indexer.rescan_schedule import (
    RETRY_S,
    SPACING_S,
    DailyRescan,
)
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles

PREFIX = "input_modules.localfiles."
HOUR = 60 * 60


class Clock:
    def __init__(self, hour):
        self.now = 0.0
        self.hour = hour


def _nightly(clock, enabled=True):
    return DailyRescan(
        enabled, monotonic=lambda: clock.now, local_hour=lambda: clock.hour
    )


@pytest.mark.parametrize(("hour", "due"), [(2, False), (3, True), (5, True), (6, False)])
def test_the_scan_runs_only_in_the_small_hours(hour, due):
    clock = Clock(hour)
    nightly = _nightly(clock)
    clock.now += 24 * HOUR
    assert nightly.due() is due


async def _scan(nightly, clock, takes=0.0, completes=True):
    async def scan():
        clock.now += takes
        return completes

    await nightly.run(scan)


@pytest.mark.asyncio
async def test_a_scan_of_any_kind_stands_for_the_next_night():
    clock = Clock(hour=4)
    nightly = _nightly(clock)
    clock.now += 24 * HOUR
    await _scan(nightly, clock)

    clock.now += SPACING_S - 1
    assert not nightly.due()
    clock.now += 1
    assert nightly.due()


@pytest.mark.asyncio
async def test_a_box_started_at_night_does_not_scan_twice():
    clock = Clock(hour=3)
    nightly = _nightly(clock)
    await _scan(nightly, clock)
    clock.now += 2 * HOUR
    assert not nightly.due()


@pytest.mark.asyncio
async def test_a_long_nightly_scan_does_not_push_out_the_next_night():
    clock = Clock(hour=3)
    nightly = _nightly(clock)
    clock.now += 24 * HOUR
    await _scan(nightly, clock, takes=5 * HOUR)

    clock.now += 19 * HOUR
    assert nightly.due()


@pytest.mark.asyncio
async def test_a_scan_that_fails_is_tried_again_that_night_but_not_at_once():
    clock = Clock(hour=3)
    nightly = _nightly(clock)
    clock.now += 24 * HOUR
    await _scan(nightly, clock, completes=False)

    assert not nightly.due()
    clock.now += RETRY_S
    assert nightly.due()


@pytest.mark.asyncio
async def test_a_scan_that_raises_does_not_stand_for_the_night():
    clock = Clock(hour=3)
    nightly = _nightly(clock)
    clock.now += 24 * HOUR

    async def scan():
        raise OSError("share gone")

    with pytest.raises(OSError):
        await nightly.run(scan)
    clock.now += RETRY_S
    assert nightly.due()


def test_switched_off_it_is_never_due():
    clock = Clock(hour=4)
    nightly = _nightly(clock, enabled=False)
    clock.now += 7 * 24 * HOUR
    assert not nightly.due()


def test_a_new_library_rescans_nightly():
    assert LocalFilesConfig().daily_rescan is True


def _started(overrides: dict) -> tuple[LocalFilesConfig, bool]:
    """The configuration the server starts the library with from these saved
    overrides, which it updates as it does on disk, and whether it had to."""
    collection = PreparedModuleCollection()
    config = collection._build_module_config(
        "localfiles", KalinkaPluginLocalFiles, overrides
    )
    return config, collection.overrides_dirty


@dataclass
class _Context:
    config: LocalFilesConfig


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


def _saved(config: LocalFilesConfig, changes: dict) -> dict:
    """What ``PUT /server/config`` keeps in the overrides file."""
    targets = ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"localfiles": _Prepared(_Context(config))},
        devices={},
    )
    kept, committed = {}, []
    for key, value in changes.items():
        target = targets.resolve(key)
        assert commit_change(target, key, value) is None
        kept[key] = stored_value(target)
        committed.append(target)
    kept.update(reconcile_written(committed))
    return kept


def test_a_saved_interval_of_0_carries_over_as_manual_scans_only():
    overrides = {PREFIX + "scan_interval_minutes": 0}

    config, dirty = _started(overrides)

    assert config.daily_rescan is False
    assert dirty and overrides[PREFIX + "daily_rescan"] is False


def test_the_carried_over_choice_is_stable_from_then_on():
    overrides = {PREFIX + "scan_interval_minutes": 0}
    _started(overrides)

    config, dirty = _started(overrides)

    assert config.daily_rescan is False and not dirty


@pytest.mark.parametrize("minutes", [1, 15, 1440])
def test_any_other_saved_interval_becomes_the_nightly_rescan(minutes):
    overrides = {PREFIX + "scan_interval_minutes": minutes}

    config, dirty = _started(overrides)

    assert config.daily_rescan is True
    assert not dirty


def test_a_box_that_never_set_an_interval_keeps_its_overrides_as_they_were():
    overrides = {PREFIX + "file_watch_enabled": False}

    config, dirty = _started(overrides)

    assert config.daily_rescan is True and not dirty
    assert overrides == {PREFIX + "file_watch_enabled": False}


def test_the_switch_once_saved_outranks_the_old_interval():
    overrides = {
        PREFIX + "scan_interval_minutes": 0,
        PREFIX + "daily_rescan": True,
    }

    config, _ = _started(overrides)

    assert config.daily_rescan is True


def test_a_saved_negative_interval_gives_way_to_the_default():
    """One saved while negatives were still accepted costs that setting only,
    not the rest of the library's."""
    config, _ = _started(
        {
            PREFIX + "scan_interval_minutes": -1,
            PREFIX + "file_watch_enabled": False,
        }
    )

    assert config.daily_rescan is True
    assert config.file_watch_enabled is False


def test_an_older_client_writing_the_interval_still_sets_the_switch():
    """An app or script from before the switch knows only the interval."""
    config = LocalFilesConfig()

    kept = _saved(config, {PREFIX + "scan_interval_minutes": 0})

    assert config.daily_rescan is False
    assert kept[PREFIX + "daily_rescan"] is False


@pytest.mark.parametrize(
    ("daily", "interval"), [(False, 0), (True, LEGACY_DAILY_INTERVAL_MINUTES)]
)
def test_saving_the_switch_leaves_a_downgrade_the_same_choice(daily, interval):
    """An older server ignores daily_rescan, and reads the interval instead."""
    kept = _saved(LocalFilesConfig(), {PREFIX + "daily_rescan": daily})

    assert kept[PREFIX + "scan_interval_minutes"] == interval


def test_the_switch_and_the_interval_it_wrote_agree_after_a_restart():
    overrides = _saved(LocalFilesConfig(), {PREFIX + "daily_rescan": False})

    config, dirty = _started(overrides)

    assert config.daily_rescan is False and not dirty


def test_the_old_interval_is_not_offered_as_a_setting():
    assert "scan_interval_minutes" not in LocalFilesConfig().model_dump()
