"""The server may set the plugin up again right after shutting it down, so
shutdown leaves nothing of the old instance running."""

import asyncio

from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo


async def test_shutdown_waits_for_the_cancelled_provisioning():
    plugin = KalinkaPluginJamendo()
    provisioning = asyncio.create_task(asyncio.Event().wait())
    plugin._provision_task = provisioning

    await plugin.shutdown()

    assert provisioning.cancelled()
    assert plugin._provision_task is None
