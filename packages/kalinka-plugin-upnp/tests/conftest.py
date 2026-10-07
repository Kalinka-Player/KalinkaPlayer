from unittest.mock import AsyncMock

import pytest
from kalinka_plugin_sdk.datamodel import DeviceVolume
from kalinka_plugin_sdk.direct_playback import OutputCapabilities, RevokeReason


class FakeHold:
    def __init__(self, listener):
        self.listener = listener
        self.active = True
        self.play = AsyncMock()
        self.set_next = AsyncMock()
        self.pause = AsyncMock()
        self.resume = AsyncMock()
        self.stop = AsyncMock()
        self.seek = AsyncMock()
        self.set_volume = AsyncMock()
        self.get_volume = AsyncMock(
            return_value=DeviceVolume(current_volume=40, max_volume=80)
        )
        self.release = AsyncMock(side_effect=self._release)

    async def _release(self):
        self.active = False

    def revoke(self, reason=RevokeReason.QUEUE_PLAY):
        self.active = False
        self.listener.on_revoked(reason)


class FakeDirect:
    def __init__(self):
        self.sessions = []
        self.acquire = AsyncMock(side_effect=self._acquire)
        self.watchers = []

    def watch_output_capabilities(self, listener):
        self.watchers.append(listener)
        listener.on_output_capabilities(OutputCapabilities())
        return lambda: self.watchers.remove(listener)

    def set_dsd(self, dsd):
        for listener in self.watchers:
            listener.on_output_capabilities(OutputCapabilities(dsd=dsd))

    async def _acquire(self, title, listener):
        hold = FakeHold(listener)
        self.sessions.append(hold)
        listener.on_volume(await hold.get_volume())
        return hold


@pytest.fixture
def direct():
    return FakeDirect()
