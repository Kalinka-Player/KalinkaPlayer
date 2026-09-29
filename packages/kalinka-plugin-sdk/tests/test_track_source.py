import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource

URL = DirectUrl(url="http://example.com/live")


def test_a_sequential_source_may_start_partway_through_its_track():
    source = TrackSource(source=URL, format="ogg", sequential=True, timeline_offset_ms=45000)
    assert source.timeline_offset_ms == 45000


def test_only_a_sequential_source_has_a_timeline_offset():
    with pytest.raises(ValidationError):
        TrackSource(source=URL, format="ogg", timeline_offset_ms=45000)
