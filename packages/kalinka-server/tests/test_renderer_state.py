from kalinka_server import renderer_state
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_state import StateChange

from tests.sim_renderer import dsd_conversion


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


def test_meters_convert_whole():
    meters = pb.DspMeters()
    meters.source_token = "7"
    meters.clipped_samples = 12
    meters.true_peak_dbfs = -0.4
    meters.load = 0.012
    meters.at_unix_ms = 1700000000500

    assert renderer_state.dsp_meters_to_dict(meters) == {
        "source_token": "7",
        "clipped_samples": 12,
        "true_peak_dbfs": -0.4,
        "load": 0.012,
        "at_unix_ms": 1700000000500,
    }
