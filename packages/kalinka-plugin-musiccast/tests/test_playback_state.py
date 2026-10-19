"""MusicCast reports volume calibration; playback correction belongs to Core."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kalinka_plugin_musiccast.config_model import KalinkaPluginMusiccastConfig
from kalinka_plugin_musiccast.musiccast import KalinkaPluginMusiccastDevice


@pytest.mark.unit
@pytest.mark.parametrize("step", [0.5, 1.0, 2.0])
async def test_reports_device_scale_without_adjusting_volume(step):
    config = KalinkaPluginMusiccastConfig(volume_step_to_db=step)
    listener = MagicMock()
    device = KalinkaPluginMusiccastDevice(config, MagicMock(), listener)
    device._get_status = AsyncMock(
        return_value={
            "max_volume": 60,
            "volume": 30,
            "power": "on",
            "input": "optical1",
        }
    )
    device.set_volume = AsyncMock()
    try:
        await device.get_ready()
        volume = await device.get_volume()
        assert volume.current_volume == 30
        assert volume.db_values == [i * step for i in range(61)]
        assert volume.volume_gain == 0
        device.set_volume.assert_not_awaited()
        listener.stream.assert_not_called()
        assert "auto_volume_correction" not in config.__class__.model_fields
    finally:
        await device.session.aclose()
