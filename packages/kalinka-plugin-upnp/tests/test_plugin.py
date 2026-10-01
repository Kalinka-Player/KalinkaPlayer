from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.module_health import ModuleHealthState
from kalinka_plugin_sdk.plugin import PluginSetupException
from kalinka_plugin_upnp import KalinkaPluginUpnp
from kalinka_plugin_upnp.config_model import UpnpConfig
from kalinka_plugin_upnp.module_setup import persistent_udn
from pydantic import ValidationError


async def test_disabled_plugin_needs_no_network_or_state_directory(monkeypatch):
    monkeypatch.setattr(
        "kalinka_plugin_upnp.module_setup.persistent_udn",
        lambda _: pytest.fail("Wrote disabled plugin state"),
    )
    plugin = KalinkaPluginUpnp()
    await plugin.setup(SimpleNamespace(config=UpnpConfig(), direct_playback=None))
    assert (await plugin.get_state()).state == ModuleHealthState.DISABLED
    assert isinstance(plugin.get_interface(), InputModule)
    assert (await plugin.get_interface().browse(None)).total == 0
    await plugin.shutdown()


async def test_direct_playback_is_required():
    plugin = KalinkaPluginUpnp()
    with pytest.raises(PluginSetupException):
        await plugin.setup(
            SimpleNamespace(config=UpnpConfig(enabled=True), direct_playback=None)
        )


async def test_plugin_starts_and_stops_receiver(monkeypatch, direct, tmp_path):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.setattr("kalinka_plugin_upnp.server.Discovery.start", AsyncMock())
    plugin = KalinkaPluginUpnp()
    await plugin.setup(
        SimpleNamespace(
            config=UpnpConfig(enabled=True, interface_address="127.0.0.1"),
            direct_playback=direct,
        )
    )
    try:
        assert (await plugin.get_state()).state == ModuleHealthState.READY
        assert plugin.receiver.udn == persistent_udn(tmp_path / "var/lib/kalinka/upnp")
        assert (
            await plugin.resolve_dynamic_field("receiver_status")
            == "Waiting for a UPnP controller"
        )
    finally:
        await plugin.shutdown()
    assert plugin.receiver is None


def test_identity_survives_restart_and_does_not_follow_device_name(tmp_path):
    assert persistent_udn(tmp_path) == persistent_udn(tmp_path)
    assert persistent_udn(tmp_path).startswith("uuid:")


@pytest.mark.parametrize(
    "address", ["0.0.0.0", "239.255.255.250", "255.255.255.255", "::1", "invalid"]
)
def test_invalid_interface_configuration(address):
    with pytest.raises(ValidationError):
        UpnpConfig(interface_address=address)
