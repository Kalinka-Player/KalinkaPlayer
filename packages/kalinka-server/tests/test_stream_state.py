"""Renderer snapshot -> the play queue's StreamState."""

from kalinka_server import renderer_state
from kalinka_server.playback_view import to_audio_info
from kalinka_server.stream_state import (
    SignalPath,
    SignalStep,
    StreamState,
    AudioGraphNodeState,
    StreamErrorSource,
    from_snapshot,
    to_stream_id,
)

from tests.sim_renderer import dsd_conversion


def test_a_playing_snapshot_carries_position_and_format():
    state = from_snapshot(
        renderer_state.empty_state()
        | {
            "playback_state": "playing",
            "position_ms": 4200,
            "format": {
                "sample_rate_hz": 44100,
                "channels": 2,
                "bits_per_sample": 24,
            },
            "duration_ms": 204_000,
        }
    )

    assert state is not None
    assert state.state is AudioGraphNodeState.STREAMING
    assert state.position == 4200
    assert state.timestamp > 0  # local receipt time, not the renderer's clock
    assert state.stream_info is not None
    assert state.stream_info.format.sample_rate == 44100
    assert state.stream_info.duration_ms == 204_000


def test_a_renderer_that_has_not_reported_yet_translates_to_nothing():
    """empty_state() is 'unspecified' — there is no queue state for it."""
    assert from_snapshot(renderer_state.empty_state()) is None


def test_errors_keep_their_source():
    state = from_snapshot(
        renderer_state.empty_state()
        | {
            "playback_state": "error",
            "error": {"source": "http_stream", "message": "404"},
        }
    )

    assert state is not None
    assert state.state is AudioGraphNodeState.ERROR
    assert state.error is not None
    assert state.error.source is StreamErrorSource.HTTP_STREAM
    assert state.error.message == "404"


def test_a_snapshot_names_the_stream_it_is_about():
    """The token is the stream id the queue minted, stringified for the wire."""
    state = from_snapshot(
        renderer_state.empty_state()
        | {"playback_state": "playing", "source_token": "7"}
    )

    assert state is not None
    assert state.stream_id == 7


def test_a_stop_says_whether_a_renderer_setting_interrupted_it():
    interrupted = from_snapshot(
        renderer_state.empty_state()
        | {"playback_state": "stopped", "interrupted": True}
    )
    assert interrupted is not None and interrupted.interrupted

    plain = from_snapshot(renderer_state.empty_state() | {"playback_state": "stopped"})
    assert plain is not None and not plain.interrupted


def test_only_a_stop_can_be_interrupted():
    """The field rides on a STOPPED alone; anything else is played as it says."""
    playing = from_snapshot(
        renderer_state.empty_state()
        | {"playback_state": "playing", "interrupted": True}
    )
    assert playing is not None and not playing.interrupted


def test_a_token_from_elsewhere_names_no_stream_of_ours():
    """Only this Core's tokens are stream ids; another Core's need not be."""
    assert to_stream_id(None) is None
    assert to_stream_id("") is None
    assert to_stream_id("f3a1-not-ours") is None


def test_a_stream_of_unknown_length_has_no_duration():
    """Absent, not zero — a length nobody knows is not a track that has ended."""
    state = from_snapshot(
        renderer_state.empty_state()
        | {"playback_state": "paused", "format": {"sample_rate_hz": 44100}}
    )

    assert state is not None
    assert state.state is AudioGraphNodeState.PAUSED
    assert state.stream_info is not None
    assert state.stream_info.duration_ms is None


def _at(state_name: AudioGraphNodeState, position: int, age_ms: int) -> int:
    now = 10_000_000_000
    state = StreamState(
        state=state_name,
        position=position,
        timestamp=now - age_ms * 1_000_000,
    )
    return state.position_at(now)


def test_a_running_stream_has_advanced_since_it_was_last_reported():
    """A renderer names a position only when something changes."""
    assert _at(AudioGraphNodeState.STREAMING, 4200, age_ms=800) == 5000


def test_nothing_else_advances():
    for state in (
        AudioGraphNodeState.PAUSED,
        AudioGraphNodeState.PREPARING,
        AudioGraphNodeState.STOPPED,
        AudioGraphNodeState.FINISHED,
    ):
        assert _at(state, 4200, age_ms=800) == 4200, state


def test_a_timestamp_from_the_future_never_rewinds_playback():
    assert _at(AudioGraphNodeState.STREAMING, 4200, age_ms=-500) == 4200


def _path(decoded: dict, device: dict | None, steps: list[dict] | None = None):
    """The lossless verdict for a stream decoded one way and played another,
    through @p steps when the renderer reported any."""
    snapshot = renderer_state.empty_state() | {
        "playback_state": "playing",
        "format": decoded,
    }
    if device is not None:
        snapshot["device_info"] = device
    if steps is not None:
        snapshot["signal_path"] = {"steps": steps, "latency_ms": 0.0}
    state = from_snapshot(snapshot)
    assert state is not None and state.stream_info is not None
    return state.stream_info.lossless_path


_CD = {"sample_rate_hz": 44100, "channels": 2, "bits_per_sample": 16}


def test_an_exclusive_device_running_the_decoded_format_is_lossless():
    assert _path(_CD, {"format": _CD, "access": "exclusive"})


def test_a_shared_device_is_never_lossless_however_well_it_matches():
    """It reports the format its plugin was opened at, which says nothing
    about what the card ends up running."""
    assert not _path(_CD, {"format": _CD, "access": "shared"})


def test_a_device_that_cannot_say_how_it_is_held_is_not_taken_on_trust():
    assert not _path(_CD, {"format": _CD, "access": "unknown"})


def test_no_device_is_no_claim():
    assert not _path(_CD, None)


def test_a_resampling_device_is_not_lossless():
    resampled = _CD | {"sample_rate_hz": 48000}
    assert not _path(_CD, {"format": resampled, "access": "exclusive"})


def test_a_device_carrying_fewer_bits_is_not_lossless():
    truncated = _CD | {"bits_per_sample": 8}
    assert not _path(_CD, {"format": truncated, "access": "exclusive"})


_CD16 = _CD | {"sample_format": "PCM16_LE"}
_S32 = _CD | {"sample_format": "PCM32_LE", "bits_per_sample": 32}


def _step(kind: str, bit_perfect: bool) -> dict:
    return {"kind": kind, "name": kind, "summary": "", "bit_perfect": bit_perfect}


def test_zero_padding_the_renderer_reports_is_lossless():
    padded = [_step("zero_padding", True)]
    assert _path(_CD16, {"format": _S32, "access": "exclusive"}, padded)


def test_a_float_round_trip_that_keeps_every_bit_is_lossless():
    """The idle room correction stage carries the integers in float and hands
    them back exactly."""
    steps = [_step("float", True), _step("volume", True), _step("integer", True)]
    assert _path(_CD16, {"format": _S32, "access": "exclusive"}, steps)


def test_a_wider_device_no_step_explains_is_not_lossless():
    """A renderer that predates the trace says nothing of how it widened."""
    assert not _path(_CD16, {"format": _S32, "access": "exclusive"})


def test_one_step_that_alters_the_samples_is_enough_to_lose_it():
    steps = [_step("float", True), _step("volume", False), _step("dither", False)]
    assert not _path(_CD16, {"format": _S32, "access": "exclusive"}, steps)


def test_a_step_from_a_renderer_without_the_mark_alters_the_samples():
    steps = [{"kind": "zero_padding", "name": "", "summary": ""}]
    assert not _path(_CD16, {"format": _S32, "access": "exclusive"}, steps)


def test_a_float_device_never_takes_the_sources_bits():
    float32 = _CD | {"sample_format": "PCM_FLOAT32_LE", "bits_per_sample": 32}
    steps = [_step("float", True)]
    assert not _path(_CD16, {"format": float32, "access": "exclusive"}, steps)


def test_a_device_opened_for_other_channels_is_not_lossless():
    """Every ALSA device is opened for stereo; a mono stream is not that."""
    assert not _path(_CD | {"channels": 1}, {"format": _CD, "access": "exclusive"})


def test_a_device_described_without_a_decoded_format_claims_nothing():
    """A renderer that names its output but not what it decoded — the browser —
    has not shown that the two agree."""
    assert not _path({}, {"format": _CD, "access": "exclusive"})


def test_a_device_and_a_stream_that_both_say_nothing_do_not_agree():
    """Exclusive access with no format either side is a claim out of nothing:
    the zeros mean unreported, not two formats that happen to match."""
    assert not _path({}, {"access": "exclusive"})


def test_dsd_source_rate_and_native_or_dop_carrier_are_lossless():
    source = {"sample_rate_hz": 2822400, "bits_per_sample": 1,
              "channels": 2, "sample_format": "DSD"}
    for name, rate, bits in [("DSD_U32_LE", 88200, 1),
                             ("DoP_S24_LE", 176400, 24),
                             ("DoP_S32_LE", 176400, 24)]:
        device = {"sample_rate_hz": rate, "bits_per_sample": bits,
                  "channels": 2, "sample_format": name}
        assert _path(source, {"format": device, "access": "exclusive"})
        assert not _path(source, {"format": device, "access": "shared"})
        assert not _path(source, {"format": device | {"sample_rate_hz": rate * 2},
                                  "access": "exclusive"})
    assert not _path(source, {"format": _CD, "access": "exclusive"})


def test_client_sees_dsd_source_and_dop_carrier_separately():
    state = from_snapshot(
        {
            "playback_state": "playing",
            "format": {"sample_rate_hz": 2822400, "bits_per_sample": 1,
                       "channels": 2, "sample_format": "DSD"},
            "device_info": {
                "access": "exclusive",
                "format": {"sample_rate_hz": 176400, "bits_per_sample": 24,
                           "channels": 2, "sample_format": "DoP_S24_LE"},
            },
        }
    )
    audio = to_audio_info(state.stream_info)
    assert audio.sample_format == "DSD"
    assert audio.output.sample_format == "DoP_S24_LE"
    assert audio.output.lossless_path


_DSD64 = {"sample_rate_hz": 2822400, "bits_per_sample": 1, "channels": 2,
          "sample_format": "DSD"}
_PCM_176 = {"sample_rate_hz": 176400, "bits_per_sample": 24, "channels": 2,
            "sample_format": "S32_LE"}
_CONVERTING_DSD = renderer_state.signal_path_to_dict(dsd_conversion())


def _converting(signal_path):
    return from_snapshot(
        renderer_state.empty_state()
        | {
            "playback_state": "playing",
            "format": _DSD64,
            "device_info": {"format": _PCM_176, "access": "exclusive"},
            "signal_path": signal_path,
        }
    )


def test_the_signal_path_rides_with_the_stream():
    state = _converting(_CONVERTING_DSD)

    assert state.stream_info.signal_path == SignalPath(
        steps=[
            SignalStep("dsd_to_pcm", "DSD to PCM",
                       "DSD64 to 176.4 kHz, 30 kHz filter"),
            SignalStep("dsd_gain", "DSD gain", "+2.8 dB"),
            SignalStep("dither", "Dither", "TPDF at 24 bits"),
        ],
        latency_ms=0.3,
    )


def test_a_step_at_the_decoded_format_is_not_lossless():
    """A step that keeps the format still changes the samples."""
    snapshot = renderer_state.empty_state() | {
        "playback_state": "playing",
        "format": _CD,
        "device_info": {"format": _CD, "access": "exclusive"},
        "signal_path": {
            "steps": [{"kind": "peq", "name": "EQ", "summary": "3 bands"}],
            "latency_ms": 0.0,
        },
    }

    assert not from_snapshot(snapshot).stream_info.lossless_path


def test_samples_left_as_decoded_have_no_signal_path():
    assert _converting(None).stream_info.signal_path is None


def test_clients_see_the_signal_path_on_the_output():
    output = to_audio_info(_converting(_CONVERTING_DSD).stream_info).output

    assert output.model_dump(mode="json")["signal_path"] == _CONVERTING_DSD
    assert not output.lossless_path


def test_clients_see_no_signal_path_where_none_was_reported():
    output = to_audio_info(_converting(None).stream_info).output

    assert output.signal_path is None
    assert output.model_dump(mode="json")["signal_path"] is None


def test_an_older_renderers_output_reads_as_it_always_has():
    """What it reported before, with nothing said of a signal path."""
    snapshot = renderer_state.empty_state() | {
        "playback_state": "playing",
        "format": _CD,
        "device_info": {"format": _CD, "access": "exclusive"},
    }
    output = to_audio_info(from_snapshot(snapshot).stream_info).output

    assert output.model_dump(mode="json") == {
        "sample_format": "",
        "sample_rate": 44100,
        "bits_per_sample": 16,
        "channels": 2,
        "access": "exclusive",
        "lossless_path": True,
        "signal_path": None,
    }
