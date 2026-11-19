"""The signal path a client is shown and the playback events it is sent."""

import json

from pydantic import BaseModel, create_model

from kalinka_plugin_sdk import (
    PlaybackEventReport,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.datamodel import (
    AudioInfo,
    OutputInfo,
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
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


def _as_before_play_ids(model: type[BaseModel]) -> type[BaseModel]:
    """``model`` as an SDK from before play ids declares it."""
    fields = {
        name: (field.annotation, field)
        for name, field in model.model_fields.items()
        if name != "play_id"
    }
    return create_model(
        f"Older{model.__name__}", __config__=model.model_config, **fields
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


def test_a_playback_event_names_its_type_play_and_kind():
    event = PlaybackEventReport(
        play_id="17", kind="clipping", details={"clipped_samples": 1200}, seq=7
    )

    assert event.event_type is PlayQueueEventType.PlaybackEvent
    assert json.loads(event.model_dump_json()) == {
        "event_type": "playback_event",
        "seq": 7,
        "play_id": "17",
        "kind": "clipping",
        "details": {"clipped_samples": 1200},
    }


def test_nothing_names_a_play_unless_told():
    assert PlaybackState().play_id is None
    assert PlaybackEventReport(kind="clipping").play_id is None


def test_the_state_names_its_play_on_the_wire():
    state = PlaybackState(state=PlayerStateEnum.PLAYING, index=0, play_id="17")

    assert json.loads(state.model_dump_json())["play_id"] == "17"
    assert PlaybackState.model_validate_json(state.model_dump_json()) == state


def test_an_older_client_reads_past_the_play_id():
    state = PlaybackState(state=PlayerStateEnum.PLAYING, index=2, play_id="17")
    event = PlaybackEventReport(
        play_id="17", kind="clipping", details={"clipped_samples": 1200}, seq=7
    )

    older_state = _as_before_play_ids(PlaybackState).model_validate_json(
        state.model_dump_json()
    )
    older_event = _as_before_play_ids(PlaybackEventReport).model_validate_json(
        event.model_dump_json()
    )

    assert older_state.model_dump() == state.model_dump(exclude={"play_id"})
    assert older_event.model_dump() == event.model_dump(exclude={"play_id"})


def test_an_older_server_names_no_play():
    state = PlaybackState.model_validate_json('{"state": "PLAYING", "index": 0}')
    event = PlaybackEventReport.model_validate_json(
        '{"event_type": "playback_event", "seq": 1, "kind": "clipping",'
        ' "details": {"clipped_samples": 3}}'
    )

    assert state.play_id is None
    assert event.play_id is None


def test_a_playback_event_of_a_kind_without_details():
    assert PlaybackEventReport(kind="clipping").details == {}
    assert (
        PlaybackEventReport(kind="clipping").details
        is not PlaybackEventReport(kind="clipping").details
    )


def test_playback_events_are_kept_out_of_the_state():
    state = _state()

    assert state.apply(PlaybackEventReport(kind="clipping", seq=1)) is state
