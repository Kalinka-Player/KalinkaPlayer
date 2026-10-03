import pytest

from kalinka_plugin_upnp.upnp import UpnpInput


async def test_the_queue_is_never_given_a_source_for_a_controller_track():
    with pytest.raises(LookupError, match="controller"):
        await UpnpInput().get_track_source("anything")
