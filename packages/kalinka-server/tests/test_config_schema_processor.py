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
