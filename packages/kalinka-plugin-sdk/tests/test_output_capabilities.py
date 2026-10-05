"""What a plugin is told the output can play before it takes the output."""

import dataclasses

import pytest

from kalinka_plugin_sdk import DirectPlayback, OutputCapabilities


def test_nothing_is_known_until_the_server_says_so():
    assert OutputCapabilities().dsd is None


def test_capabilities_are_a_value_a_plugin_cannot_change():
    capabilities = OutputCapabilities(dsd=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        capabilities.dsd = False


def test_direct_playback_offers_the_question():
    assert "output_capabilities" in DirectPlayback.__dict__
