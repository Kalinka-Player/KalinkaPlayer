"""A plugin whose REQUIRES_SDK excludes the installed SDK is never set up.

When an SDK major lands, bootstrap installs the new SDK and pip refuses the
wheel of a plugin still pinned to the old one, but the copy an earlier boot
installed stays in the venv. Loaded against the new SDK it either failed on an
import, which was only logged, or ran on a contract it was not written for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from packaging.version import Version
from pydantic import Field

from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginBase, PluginType

from kalinka_server.player_setup import PreparedModuleCollection

MAJOR = Version(SDK_VERSION).major
THIS_MAJOR = f">={MAJOR},<{MAJOR + 1}"
LAST_MAJOR = f">={MAJOR - 1},<{MAJOR}"


class _Config(ModuleConfig):
    enabled: bool = Field(default=True)
    rebuild: bool = Field(default=False, json_schema_extra={"one_shot": True})


def _plugin(plugin_id: str, requires_sdk: str):
    class _Plugin(PluginBase):
        PLUGIN_ID = plugin_id
        REQUIRES_SDK = requires_sdk
        PLUGIN_TYPE = PluginType.INPUT_MODULE
        CONFIG_MODEL = _Config
        set_up = False

        async def setup(self, context):
            type(self).set_up = True

    return _Plugin


async def _load(plugins, overrides):
    collection = PreparedModuleCollection()
    collection._scan_entry_points = lambda: iter(  # type: ignore[method-assign]
        [(p.PLUGIN_ID, p) for p in plugins]
    )
    collection._make_plugin_context = (  # type: ignore[method-assign]
        lambda name, cls, config: SimpleNamespace(config=config)
    )
    return dict(await collection._scan_and_setup_plugins_from_entry_points(overrides))


@pytest.mark.asyncio
async def test_a_plugin_for_the_last_sdk_major_is_listed_as_an_error_and_not_set_up():
    stale, current = _plugin("stale", LAST_MAJOR), _plugin("current", THIS_MAJOR)

    loaded = await _load([stale, current], {})

    assert stale.set_up is False
    assert loaded["stale"].health_state == ModuleHealthState.ERROR
    assert loaded["stale"].plugin_instance is None
    assert SDK_VERSION in (loaded["stale"].error_message or "")
    assert current.set_up is True
    assert loaded["current"].health_state == ModuleHealthState.READY


@pytest.mark.asyncio
async def test_a_plugin_that_is_not_set_up_keeps_its_one_shot_trigger_armed():
    stale, current = _plugin("stale", LAST_MAJOR), _plugin("current", THIS_MAJOR)
    overrides = {
        "input_modules.stale.rebuild": True,
        "input_modules.current.rebuild": True,
    }

    await _load([stale, current], overrides)

    assert overrides == {"input_modules.stale.rebuild": True}
