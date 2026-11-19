"""The signal path a client is shown and the DSP meters it is sent."""

import json

from kalinka_plugin_sdk import (
    DspMetersChangedEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.datamodel import (
    AudioInfo,
    DspMeters,
    OutputInfo,
    PlaybackMode,
    PlaybackState,
    SignalPath,
    SignalStep,
)

CONVERTING_DSD = SignalPath(
    steps=[
        SignalStep(
            kind="dsd_to_pcm",
            name="DSD to PCM",
            summary="DSD64 to 176.4 kHz, 30 kHz filter",
        ),
        SignalStep(kind="dsd_gain", name="DSD gain", summary="+2.8 dB"),
        SignalStep(kind="dither", name="Dither", summary="TPDF at 24 bits"),
    ],
    latency_ms=0.3,
)


def _state() -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
    )


def test_an_output_states_no_signal_path_unless_told():
    assert OutputInfo().signal_path is None


def test_a_signal_path_starts_empty():
    assert SignalPath() == SignalPath(steps=[], latency_ms=0.0)
    assert SignalPath().steps is not SignalPath().steps
    assert SignalStep() == SignalStep(kind="", name="", summary="")


def test_the_signal_path_survives_the_wire():
    audio = AudioInfo(
        sample_format="DSD",
        sample_rate=2822400,
        bits_per_sample=1,
        channels=2,
        duration_ms=0,
        output=OutputInfo(
            sample_format="S32_LE",
            sample_rate=176400,
            bits_per_sample=24,
            channels=2,
            signal_path=CONVERTING_DSD,
        ),
    )

    restored = AudioInfo.model_validate_json(audio.model_dump_json())

    assert restored.output.signal_path == CONVERTING_DSD


def test_an_output_from_an_older_server_reads_without_one():
    restored = OutputInfo.model_validate_json(
        json.dumps({"sample_rate": 44100, "channels": 2, "lossless_path": True})
    )

    assert restored.signal_path is None


def test_meters_before_any_sound():
    assert DspMeters() == DspMeters(clipped_samples=0, true_peak_dbfs=-200.0, load=0.0)


def test_the_meters_event_names_its_type():
    event = DspMetersChangedEvent(meters=DspMeters(clipped_samples=1))

    assert event.event_type is PlayQueueEventType.DspMetersChanged
    assert json.loads(event.model_dump_json())["event_type"] == "dsp_meters_changed"


def test_meters_are_kept_out_of_the_state():
    state = _state()

    assert state.apply(DspMetersChangedEvent(meters=DspMeters(), seq=1)) is state
