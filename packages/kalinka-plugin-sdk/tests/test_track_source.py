import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource
from kalinka_plugin_sdk.datamodel import PlaybackState

URL = DirectUrl(url="http://example.com/live")


def test_codec_metadata_is_optional_and_independent_of_the_source_format():
    legacy = TrackSource(source=URL, format="audio/flac")
    assert legacy.audio_codec is None
    assert PlaybackState.model_validate({"mime_type": "audio/flac"}).audio_codec is None

    source = TrackSource(source=URL, format="application/dash+xml", audio_codec="flac")
    assert TrackSource.model_validate_json(source.model_dump_json()) == source
    state = PlaybackState(mime_type=source.format, audio_codec=source.audio_codec)
    assert state.model_dump()["mime_type"] == "application/dash+xml"
    assert (
        PlaybackState.model_validate_json(state.model_dump_json()).audio_codec == "flac"
    )


def test_a_sequential_source_may_start_partway_through_its_track():
    source = TrackSource(
        source=URL, format="ogg", sequential=True, timeline_offset_ms=45000
    )
    assert source.timeline_offset_ms == 45000


def test_only_a_sequential_source_has_a_timeline_offset():
    with pytest.raises(ValidationError):
        TrackSource(source=URL, format="ogg", timeline_offset_ms=45000)
