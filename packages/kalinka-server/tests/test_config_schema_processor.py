"""
Unit tests for config_schema_processor module.

Minimal tests focusing on the get_field_value function.
"""

import pytest
from enum import Enum
from pydantic import BaseModel, Field

from kalinka_server.config_overrides import set_by_path as set_field_value
from kalinka_server.config_schema_processor import get_field_value


class AudioFormat(Enum):
    """Test enum for audio formats."""

    MP3 = "mp3"
    FLAC = "flac"
    WAV = "wav"


class AudioSettings(BaseModel):
    """Nested audio configuration model."""

    volume: float = Field(default=0.8, title="Volume Level")
    format: AudioFormat = Field(default=AudioFormat.MP3, title="Audio Format")


class PlayerConfig(BaseModel):
    """Main configuration model with nested structures."""

    name: str = Field(default="Test Player", title="Player Name")
    enabled: bool = Field(default=True, title="Player Enabled")
    audio: AudioSettings = Field(default_factory=AudioSettings, title="Audio Settings")


@pytest.fixture
def player_config():
    """Fixture providing a PlayerConfig instance for testing."""
    return PlayerConfig(
        name="Test Player",
        enabled=True,
        audio=AudioSettings(volume=0.8, format=AudioFormat.MP3),
    )


class TestGetFieldValue:
    """Test class for get_field_value function."""

    def test_get_simple_field(self, player_config):
        """Test getting a simple field value."""
        assert get_field_value(player_config, ["name"]) == "Test Player"
        assert get_field_value(player_config, ["enabled"]) is True

    def test_get_nested_field(self, player_config):
        """Test getting a nested field value."""
        assert get_field_value(player_config, ["audio", "volume"]) == 0.8
        assert get_field_value(player_config, ["audio", "format"]) == AudioFormat.MP3

    def test_invalid_field_path_raises_error(self, player_config):
        """Test that invalid field path raises AttributeError."""
        with pytest.raises(AttributeError):
            get_field_value(player_config, ["nonexistent_field"])


class TestSetFieldValue:
    """Test class for set_field_value function."""

    def test_set_simple_field(self, player_config):
        """Test setting a simple field value."""
        set_field_value(player_config, ["name"], "New Player")
        assert player_config.name == "New Player"

        set_field_value(player_config, ["enabled"], False)
        assert player_config.enabled is False

    def test_set_nested_field(self, player_config):
        """Test setting a nested field value."""
        set_field_value(player_config, ["audio", "volume"], 0.5)
        assert player_config.audio.volume == 0.5

        set_field_value(player_config, ["audio", "format"], AudioFormat.FLAC)
        assert player_config.audio.format == AudioFormat.FLAC

    def test_invalid_field_path_raises_error(self, player_config):
        """Test that invalid field path raises ValueError."""
        with pytest.raises(ValueError, match="object has no field"):
            set_field_value(player_config, ["nonexistent_field"], "value")


def test_a_field_read_once_at_startup_says_a_save_needs_a_restart():
    from kalinka_server.config_model import KalinkaConfig
    from kalinka_server.config_schema_processor import build_presentation
    from kalinka_server.presentation_schema import Apply

    schema = build_presentation(
        base_config=KalinkaConfig(),
        input_modules={},
        devices={},
        input_modules_with_errors={},
        devices_with_errors={},
        dynamic_field_registry={},
    )
    by_path = {field.path: field for field in schema.expert_fields}

    assert by_path["base_config.server.port"].apply is Apply.RESTART_REQUIRED
    assert by_path["base_config.embedding.model_dir"].apply is Apply.RESTART_REQUIRED
    assert by_path["base_config.device_automation.auto_power_on"].apply is Apply.INSTANT
