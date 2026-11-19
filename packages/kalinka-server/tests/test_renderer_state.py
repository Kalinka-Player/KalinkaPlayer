import json

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from kalinka_plugin_sdk.events import PlaybackEventReport

from kalinka_server import renderer_state
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_state import StateChange

from tests.sim_renderer import clipping, dsd_conversion, of_a_newer_kind


def full_snapshot():
    snapshot = pb.StateSnapshot()
    snapshot.playback_state = pb.PLAYBACK_STATE_PLAYING
    snapshot.current_source.uri = "http://core/stream/1"
    snapshot.current_source.mime_type = "audio/flac"
    snapshot.current_source.source_token = "track-1"
    snapshot.format.sample_rate_hz = 44100
    snapshot.format.channels = 2
    snapshot.format.bits_per_sample = 24
    snapshot.format.sample_format = "S24_LE"
    snapshot.duration_ms = 204_000
    snapshot.device_info.format.sample_rate_hz = 44100
    snapshot.device_info.format.bits_per_sample = 24
    snapshot.device_info.format.sample_format = "S32_LE"
    snapshot.device_info.access = pb.DEVICE_ACCESS_EXCLUSIVE
    snapshot.position_ms = 1000
    snapshot.position_valid = True
    snapshot.captured_at_unix_ms = 1700000000000
    snapshot.volume.supported = True
    snapshot.volume.current = 55
    snapshot.volume.max = 100
    snapshot.volume.backend = pb.VOLUME_BACKEND_SOFTWARE
    snapshot.selected_device_id = "hw:CARD=sofhdadsp,DEV=0"
    snapshot.queued_source_tokens.append("track-2")
    return snapshot


def test_snapshot_replaces_the_whole_state():
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )

    assert state["playback_state"] == "playing"
    assert state["current_source"]["uri"] == "http://core/stream/1"
    assert state["source_token"] == "track-1"
    assert state["format"]["sample_format"] == "S24_LE"
    assert state["duration_ms"] == 204_000
    assert state["device_info"]["format"]["sample_format"] == "S32_LE"
    assert state["device_info"]["access"] == "exclusive"
    assert state["volume"]["backend"] == "software"
    assert state["selected_device_id"] == "hw:CARD=sofhdadsp,DEV=0"
    assert state["queued_source_tokens"] == ["track-2"]
    assert state["updated_at_unix_ms"] == 1700000000000


def test_absent_optionals_read_as_none():
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, pb.StateSnapshot()
    )

    assert state["current_source"] is None
    assert state["source_token"] is None
    assert state["format"] is None
    assert state["selected_device_id"] is None
    assert state["error"] is None
    assert state["playback_state"] == "unspecified"


def test_a_new_source_drops_the_descriptor_of_the_old_one():
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )

    changed = pb.SourceChanged()
    changed.source_token = "track-2"
    changed.previous_source_token = "track-1"
    changed.at_unix_ms = 1700000005000
    state = renderer_state.apply(state, StateChange.SOURCE, changed)

    # The event carries a token; the descriptor for it has not been reported.
    assert state["source_token"] == "track-2"
    assert state["current_source"] is None
    assert state["playback_state"] == "playing"  # untouched by this message
    # The old track's format and length must not be read as the new source's.
    assert state["format"] is None
    assert state["duration_ms"] is None


def test_changes_patch_only_their_own_fields():
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )

    volume = pb.VolumeChanged()
    volume.volume.supported = True
    volume.volume.current = 70
    volume.volume.max = 100
    volume.volume.backend = pb.VOLUME_BACKEND_HARDWARE
    volume.external = True
    state = renderer_state.apply(state, StateChange.VOLUME, volume)

    assert state["volume"] == {
        "supported": True,
        "current": 70,
        "max": 100,
        "backend": "hardware",
        "db_values": [],
        "control_mode": "",
    }
    assert state["selected_device_id"] == "hw:CARD=sofhdadsp,DEV=0"  # untouched
    assert state["format"]["sample_rate_hz"] == 44100  # untouched
    assert state["current_source"]["source_token"] == "track-1"  # still held


def test_errors_arrive_both_ways():
    state = renderer_state.empty_state()

    failure = pb.PlaybackError()
    failure.error.source = pb.ERROR_SOURCE_HTTP_STREAM
    failure.error.message = "404 from the stream URL"
    failure.error.source_token = "track-1"
    state = renderer_state.apply(state, StateChange.ERROR, failure)

    assert state["error"] == {
        "source": "http_stream",
        "message": "404 from the stream URL",
        "source_token": "track-1",
    }

    changed = pb.PlaybackStateChanged()
    changed.state = pb.PLAYBACK_STATE_ERROR
    changed.error.source = pb.ERROR_SOURCE_DECODER
    changed.error.message = "flac decoder gave up"
    state = renderer_state.apply(state, StateChange.PLAYBACK, changed)

    assert state["playback_state"] == "error"
    assert state["error"]["source"] == "decoder"
    assert state["error"]["source_token"] is None

    # A state change without an error clears the one that was there.
    recovered = pb.PlaybackStateChanged()
    recovered.state = pb.PLAYBACK_STATE_STOPPED
    state = renderer_state.apply(state, StateChange.PLAYBACK, recovered)

    assert state["error"] is None


def test_a_playback_state_replaces_the_format_rather_than_merging_it():
    """A state that names no format has none: nothing here remembers one."""
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )
    assert state["format"]["sample_rate_hz"] == 44100

    playing = pb.PlaybackStateChanged()
    playing.state = pb.PLAYBACK_STATE_PLAYING
    playing.format.sample_rate_hz = 96000
    state = renderer_state.apply(state, StateChange.PLAYBACK, playing)
    assert state["format"]["sample_rate_hz"] == 96000

    stopped = pb.PlaybackStateChanged()
    stopped.state = pb.PLAYBACK_STATE_STOPPED
    state = renderer_state.apply(state, StateChange.PLAYBACK, stopped)
    assert state["format"] is None


def test_the_device_is_reported_apart_from_the_decoded_format():
    """What the stream is and what the device took are separate facts: a
    device may widen the sample format, and only the pair shows it."""
    playing = pb.PlaybackStateChanged()
    playing.state = pb.PLAYBACK_STATE_PLAYING
    playing.format.sample_rate_hz = 44100
    playing.format.sample_format = "S24_LE"
    playing.device_info.format.sample_rate_hz = 44100
    playing.device_info.format.sample_format = "S32_LE"
    playing.device_info.access = pb.DEVICE_ACCESS_SHARED

    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.PLAYBACK, playing
    )

    assert state["format"]["sample_format"] == "S24_LE"
    assert state["device_info"]["format"]["sample_format"] == "S32_LE"
    assert state["device_info"]["access"] == "shared"


def test_an_interrupted_stop_is_told_by_that_state_alone():
    """It speaks for one STOPPED: the next state, or a snapshot, clears it."""
    state = renderer_state.empty_state()
    assert state["interrupted"] is False

    stopped = pb.PlaybackStateChanged()
    stopped.state = pb.PLAYBACK_STATE_STOPPED
    stopped.interrupted = True
    state = renderer_state.apply(state, StateChange.PLAYBACK, stopped)
    assert state["interrupted"] is True

    preparing = pb.PlaybackStateChanged()
    preparing.state = pb.PLAYBACK_STATE_PREPARING
    state = renderer_state.apply(state, StateChange.PLAYBACK, preparing)
    assert state["interrupted"] is False

    state = renderer_state.apply(state, StateChange.PLAYBACK, stopped)
    state = renderer_state.apply(state, StateChange.SNAPSHOT, full_snapshot())
    assert state["interrupted"] is False


def test_a_closed_device_reports_nothing():
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )
    assert state["device_info"] is not None

    stopped = pb.PlaybackStateChanged()
    stopped.state = pb.PLAYBACK_STATE_STOPPED
    state = renderer_state.apply(state, StateChange.PLAYBACK, stopped)

    assert state["device_info"] is None


CONVERTING_DSD = {
    "steps": [
        {
            "kind": "dsd_to_pcm",
            "name": "DSD to PCM",
            "summary": "DSD64 to 176.4 kHz, 30 kHz filter",
        },
        {"kind": "dsd_gain", "name": "DSD gain", "summary": "+2.8 dB"},
        {"kind": "dither", "name": "Dither", "summary": "TPDF at 24 bits"},
    ],
    "latency_ms": 0.3,
}


def test_a_snapshot_carries_the_signal_path():
    snapshot = full_snapshot()
    snapshot.signal_path.CopyFrom(dsd_conversion())

    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, snapshot
    )

    assert state["signal_path"] == CONVERTING_DSD


def test_a_playback_state_carries_the_signal_path():
    playing = pb.PlaybackStateChanged()
    playing.state = pb.PLAYBACK_STATE_PLAYING
    playing.signal_path.CopyFrom(dsd_conversion())

    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.PLAYBACK, playing
    )

    assert state["signal_path"] == CONVERTING_DSD


def test_samples_left_as_decoded_have_no_signal_path():
    """What an older renderer always sends, and a newer one doing nothing."""
    assert renderer_state.empty_state()["signal_path"] is None

    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, full_snapshot()
    )
    assert state["signal_path"] is None

    playing = pb.PlaybackStateChanged()
    playing.state = pb.PLAYBACK_STATE_PLAYING
    state = renderer_state.apply(state, StateChange.PLAYBACK, playing)
    assert state["signal_path"] is None


def test_a_playback_state_replaces_the_signal_path_rather_than_merging_it():
    converting = pb.PlaybackStateChanged()
    converting.state = pb.PLAYBACK_STATE_PLAYING
    converting.signal_path.CopyFrom(dsd_conversion())
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.PLAYBACK, converting
    )

    dithering = pb.PlaybackStateChanged()
    dithering.state = pb.PLAYBACK_STATE_PLAYING
    step = dithering.signal_path.steps.add()
    step.kind = "dither"
    state = renderer_state.apply(state, StateChange.PLAYBACK, dithering)
    assert [s["kind"] for s in state["signal_path"]["steps"]] == ["dither"]
    assert state["signal_path"]["latency_ms"] == 0.0

    untouched = pb.PlaybackStateChanged()
    untouched.state = pb.PLAYBACK_STATE_PLAYING
    state = renderer_state.apply(state, StateChange.PLAYBACK, untouched)
    assert state["signal_path"] is None


def test_a_new_source_drops_the_signal_path_of_the_old_one():
    snapshot = full_snapshot()
    snapshot.signal_path.CopyFrom(dsd_conversion())
    state = renderer_state.apply(
        renderer_state.empty_state(), StateChange.SNAPSHOT, snapshot
    )

    changed = pb.SourceChanged()
    changed.source_token = "track-2"
    state = renderer_state.apply(state, StateChange.SOURCE, changed)

    assert state["signal_path"] is None


def test_a_playback_event_converts_with_its_kind_named():
    assert renderer_state.playback_event_to_dict(clipping(12, "7")) == {
        "source_token": "7",
        "at_unix_ms": 1700000000000,
        "kind": "clipping",
        "details": {"clipped_samples": 12},
    }


def test_a_count_too_large_for_a_double_stays_an_exact_int():
    count = 2**64 - 1

    details = renderer_state.playback_event_to_dict(clipping(count, "7"))["details"]

    assert details == {"clipped_samples": count}
    assert type(details["clipped_samples"]) is int


def test_a_field_left_unset_is_there_as_its_default():
    event = pb.PlaybackEvent()
    event.clipping.SetInParent()

    assert renderer_state.playback_event_to_dict(event)["details"] == {
        "clipped_samples": 0
    }


def test_a_playback_event_about_no_source_names_none():
    event = renderer_state.playback_event_to_dict(clipping(1, None))

    assert event["source_token"] is None


def test_a_kind_unknown_here_converts_to_nothing():
    assert renderer_state.playback_event_to_dict(of_a_newer_kind("7")) is None


def test_every_kind_the_protocol_has_converts_whole():
    for kind in pb.PlaybackEvent.DESCRIPTOR.oneofs_by_name["kind"].fields:
        event = pb.PlaybackEvent()
        getattr(event, kind.name).SetInParent()

        converted = renderer_state.playback_event_to_dict(event)

        assert converted["kind"] == kind.name
        assert set(converted["details"]) == set(kind.message_type.fields_by_name)


def test_any_message_converts_as_the_state_does():
    snapshot = full_snapshot()
    snapshot.volume.db_values.extend([-200.0, -12.5, 0.0])

    assert renderer_state.message_to_dict(dsd_conversion()) == CONVERTING_DSD
    assert renderer_state.message_to_dict(
        snapshot.volume
    ) == renderer_state.volume_to_dict(snapshot.volume)


def _later_protocol() -> descriptor_pb2.FileDescriptorProto:
    """PlaybackEvent as a later renderer might have it: a second kind, whose
    details use every shape a field can take."""
    field = descriptor_pb2.FieldDescriptorProto
    file = descriptor_pb2.FileDescriptorProto(
        name="later.proto", package="later", syntax="proto3"
    )
    level = file.enum_type.add(name="BufferLevel")
    level.value.add(name="BUFFER_LEVEL_UNSPECIFIED", number=0)
    level.value.add(name="BUFFER_LEVEL_LOW", number=1)

    stage = file.message_type.add(name="Stage")
    stage.field.add(name="name", number=1, type=field.TYPE_STRING)
    stage.field.add(name="held_frames", number=2, type=field.TYPE_UINT64)

    clipping = file.message_type.add(name="Clipping")
    clipping.field.add(name="clipped_samples", number=1, type=field.TYPE_UINT64)

    buffering = file.message_type.add(name="Buffering")
    entry = buffering.nested_type.add(name="ByChannelEntry")
    entry.options.map_entry = True
    entry.field.add(name="key", number=1, type=field.TYPE_STRING)
    entry.field.add(name="value", number=2, type=field.TYPE_INT64)
    buffering.oneof_decl.add(name="_target_ms")
    repeated = field.LABEL_REPEATED
    buffering.field.add(name="ahead_frames", number=1, type=field.TYPE_UINT64)
    buffering.field.add(
        name="stages",
        number=2,
        type=field.TYPE_MESSAGE,
        type_name=".later.Stage",
        label=repeated,
    )
    buffering.field.add(
        name="slowest", number=3, type=field.TYPE_MESSAGE, type_name=".later.Stage"
    )
    buffering.field.add(
        name="starved", number=4, type=field.TYPE_MESSAGE, type_name=".later.Stage"
    )
    buffering.field.add(
        name="level", number=5, type=field.TYPE_ENUM, type_name=".later.BufferLevel"
    )
    buffering.field.add(
        name="by_channel",
        number=6,
        type=field.TYPE_MESSAGE,
        type_name=".later.Buffering.ByChannelEntry",
        label=repeated,
    )
    buffering.field.add(name="raw", number=7, type=field.TYPE_BYTES)
    buffering.field.add(name="peaks", number=8, type=field.TYPE_DOUBLE, label=repeated)
    buffering.field.add(
        name="target_ms",
        number=9,
        type=field.TYPE_DOUBLE,
        oneof_index=0,
        proto3_optional=True,
    )
    buffering.field.add(
        name="levels",
        number=10,
        type=field.TYPE_ENUM,
        type_name=".later.BufferLevel",
        label=repeated,
    )

    event = file.message_type.add(name="PlaybackEvent")
    event.oneof_decl.add(name="kind")
    event.oneof_decl.add(name="_source_token")
    event.field.add(
        name="source_token",
        number=1,
        type=field.TYPE_STRING,
        oneof_index=1,
        proto3_optional=True,
    )
    event.field.add(name="at_unix_ms", number=2, type=field.TYPE_INT64)
    event.field.add(
        name="clipping",
        number=10,
        type=field.TYPE_MESSAGE,
        type_name=".later.Clipping",
        oneof_index=0,
    )
    event.field.add(
        name="buffering",
        number=11,
        type=field.TYPE_MESSAGE,
        type_name=".later.Buffering",
        oneof_index=0,
    )
    return file


def test_a_kind_added_later_converts_with_no_change_here():
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(_later_protocol().SerializeToString())
    event = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("later.PlaybackEvent")
    )()
    event.source_token = "7"
    event.at_unix_ms = 1700000000500
    buffering = event.buffering
    buffering.ahead_frames = 2**53 + 1
    buffering.stages.add(name="dsd_to_pcm", held_frames=64)
    buffering.stages.add(name="dither")
    buffering.slowest.name = "dsd_to_pcm"
    buffering.level = 1
    buffering.by_channel["left"] = -3
    buffering.raw = b"\x00\xff"
    buffering.peaks.extend([-0.5, -1.0])
    buffering.levels.extend([1, 7])
    details = {
        "ahead_frames": 2**53 + 1,
        "stages": [
            {"name": "dsd_to_pcm", "held_frames": 64},
            {"name": "dither", "held_frames": 0},
        ],
        "slowest": {"name": "dsd_to_pcm", "held_frames": 0},
        "starved": None,
        "level": "low",
        "by_channel": {"left": -3},
        "raw": "AP8=",
        "peaks": [-0.5, -1.0],
        "target_ms": None,
        "levels": ["low", 7],
    }

    converted = renderer_state.playback_event_to_dict(event)

    assert converted == {
        "source_token": "7",
        "at_unix_ms": 1700000000500,
        "kind": "buffering",
        "details": details,
    }
    report = PlaybackEventReport(kind=converted["kind"], details=converted["details"])
    assert json.loads(report.model_dump_json())["details"] == details
