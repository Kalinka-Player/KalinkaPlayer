import asyncio
from unittest.mock import Mock

import pytest
from kalinka_plugin_sdk.datamodel import DeviceVolume, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import (
    OutputUnavailable,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_upnp.media import Media, UpnpError
from kalinka_plugin_upnp.playback import Playback


@pytest.fixture
async def playback(direct):
    playback = Playback(direct, Mock())
    playback.start()
    yield playback
    await playback.close()


async def load(playback, name="first"):
    media = Media.parse(f"http://media.test/{name}.flac", "")
    await playback.command("set_uri", media)
    return media


async def drain(playback):
    for _ in range(20):
        await asyncio.sleep(0)
        if playback.commands.empty() and not playback.lock.locked():
            return
    pytest.fail("Commands did not drain")


async def test_load_does_not_acquire_until_play_and_stop_retains_session(
    playback, direct
):
    media = await load(playback)
    direct.acquire.assert_not_called()
    await playback.command("play")
    hold = direct.sessions[0]
    hold.play.assert_awaited_once_with(media.source, media.track, start_offset_ms=0)
    await playback.command("stop")
    hold.stop.assert_awaited_once()
    hold.release.assert_not_awaited()
    assert playback.active
    assert playback.current == media
    assert playback.state == "STOPPED"


async def test_kalinka_controls_use_same_hold(playback, direct):
    await load(playback)
    await playback.command("play")
    hold = direct.sessions[0]
    hold.listener.on_state(PlaybackState(state=PlayerStateEnum.PLAYING, position=12000))
    assert playback.position_ms == 12000
    hold.listener.on_command(TransportRequest(TransportKind.PAUSE))
    await drain(playback)
    hold.pause.assert_awaited_once()
    hold.listener.on_command(TransportRequest(TransportKind.RESUME))
    await drain(playback)
    hold.resume.assert_awaited_once()
    hold.listener.on_command(TransportRequest(TransportKind.SEEK, 42500))
    await drain(playback)
    hold.seek.assert_awaited_once_with(42500)
    assert direct.acquire.await_count == 1


async def test_queued_next_starts_without_replaying_and_eof_retains_session(
    playback, direct
):
    first = await load(playback)
    second = Media.parse("http://media.test/second.mp3", "")
    await playback.command("play")
    await playback.command("set_next", second)
    hold = direct.sessions[0]
    hold.set_next.assert_awaited_once_with(second.source, second.track)
    hold.listener.on_next_started(second.track)
    await drain(playback)
    assert playback.current == second
    assert playback.previous == first
    assert playback.next is None
    assert hold.play.await_count == 1
    hold.listener.on_finished()
    await drain(playback)
    assert playback.active
    assert playback.state == "STOPPED"
    hold.stop.assert_awaited_once()
    hold.release.assert_not_awaited()


async def test_revoked_and_queued_callbacks_cannot_retake_queue(playback, direct):
    await load(playback)
    await playback.command("play")
    await playback.command("set_next", Media.parse("http://media.test/next.flac", ""))
    old = direct.sessions[0]
    old.listener.on_finished()
    old.listener.on_command(TransportRequest(TransportKind.RESUME))
    old.revoke()
    await drain(playback)
    assert not playback.active
    assert playback.state == "STOPPED"
    assert playback.next is None
    assert direct.acquire.await_count == 1
    await playback.command("play")
    old.listener.on_state(PlaybackState(state=PlayerStateEnum.ERROR))
    old.listener.on_revoked(None)
    old.listener.on_finished()
    old.listener.on_volume(DeviceVolume(current_volume=1, max_volume=100))
    await drain(playback)
    assert playback.active
    assert playback.state == "TRANSITIONING"
    assert playback.volume == 50
    assert direct.acquire.await_count == 2


async def test_seek_while_stopped_sets_initial_offset_without_taking_output(
    playback, direct
):
    media = await load(playback)
    await playback.command("seek", 12345)
    direct.acquire.assert_not_called()
    await playback.command("play")
    direct.sessions[0].play.assert_awaited_once_with(
        media.source, media.track, start_offset_ms=12345
    )


async def test_loading_new_uri_keeps_playing_on_existing_output(playback, direct):
    await load(playback)
    await playback.command("play")
    await load(playback, "replacement")
    direct.sessions[0].release.assert_not_called()
    assert direct.sessions[0].play.await_count == 2
    assert playback.active
    assert playback.state == "TRANSITIONING"


async def test_previous_and_next_commands_preserve_paused_state(playback, direct):
    first = await load(playback)
    await playback.command("play")
    await playback.command("pause")
    second = Media.parse("http://media.test/second.mp3", "")
    await playback.command("set_next", second)
    await playback.command("next")
    assert playback.current == second
    assert playback.state == "PAUSED_PLAYBACK"
    await playback.command("previous")
    assert playback.current == first
    assert playback.state == "PAUSED_PLAYBACK"
    assert direct.acquire.await_count == 1


async def test_volume_scaling_echo_and_mute(playback, direct):
    with pytest.raises(UpnpError):
        await playback.command("set_volume", 30)
    direct.acquire.assert_not_called()
    await load(playback)
    await playback.command("play")
    hold = direct.sessions[0]
    assert playback.volume == 50
    await playback.command("set_mute", True)
    hold.set_volume.assert_awaited_with(0)
    hold.listener.on_volume(DeviceVolume(current_volume=0, max_volume=80))
    assert playback.muted
    await playback.command("set_mute", False)
    hold.set_volume.assert_awaited_with(50)
    assert not playback.muted
    await playback.command("set_volume", 25)
    hold.set_volume.assert_awaited_with(25)
    hold.listener.on_volume(DeviceVolume(current_volume=24, max_volume=80))
    assert playback.volume == 30


async def test_unavailable_output_is_retryable(playback, direct):
    await load(playback)
    direct.acquire.side_effect = OutputUnavailable("No renderer")
    with pytest.raises(UpnpError) as error:
        await playback.command("play")
    assert error.value.code == 501
    assert playback.listener is None
    assert playback.status == "ERROR_OCCURRED"
    direct.acquire.side_effect = direct._acquire
    await playback.command("play")
    assert playback.active
    assert playback.status == "OK"


async def test_play_failure_releases_new_hold(playback, direct):
    await load(playback)
    original = direct._acquire

    async def acquire(title, listener):
        hold = await original(title, listener)
        hold.play.side_effect = OutputUnavailable("Gone")
        return hold

    direct.acquire.side_effect = acquire
    with pytest.raises(UpnpError):
        await playback.command("play")
    assert not playback.active
    direct.sessions[0].release.assert_awaited_once()


async def test_close_releases_and_rejects_new_commands(playback, direct):
    await load(playback)
    await playback.command("play")
    await playback.close()
    direct.sessions[0].release.assert_awaited_once()
    with pytest.raises(UpnpError):
        await playback.command("play")


async def test_cancelled_acquisition_releases_the_eventual_hold(playback, direct):
    await load(playback)
    opening, finish = asyncio.Event(), asyncio.Event()
    original = direct._acquire

    async def acquire(title, listener):
        opening.set()
        await finish.wait()
        return await original(title, listener)

    direct.acquire.side_effect = acquire
    task = asyncio.create_task(playback.command("play"))
    await opening.wait()
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    direct.sessions[0].release.assert_awaited_once()
    assert not playback.active
    assert playback.listener is None


async def test_finished_command_cannot_skip_a_replacement_track(playback, direct):
    await load(playback)
    await playback.command("play")
    direct.sessions[0].listener.on_finished()
    await load(playback, "replacement")
    await drain(playback)
    assert playback.current.track.title == "replacement.flac"
    assert playback.active


async def test_clear_uri_releases_and_clears_next(playback, direct):
    await load(playback)
    await playback.command("play")
    await playback.command("set_next", Media.parse("http://media.test/next.flac", ""))
    await playback.command("set_uri", None)
    direct.sessions[0].release.assert_awaited_once()
    assert playback.current is None
    assert playback.next is None
    assert playback.state == "NO_MEDIA_PRESENT"


async def test_position_extrapolates_only_while_playing(playback, monkeypatch):
    await load(playback)
    monkeypatch.setattr(
        "kalinka_plugin_upnp.playback.time.monotonic_ns", lambda: 5_000_000_000
    )
    playback.update_state(
        PlaybackState(
            state=PlayerStateEnum.PLAYING, position=2000, timestamp_ns=2_000_000_000
        )
    )
    assert playback.position_now_ms() == 5000
    playback.update_state(
        PlaybackState(
            state=PlayerStateEnum.PAUSED, position=2000, timestamp_ns=2_000_000_000
        )
    )
    assert playback.position_now_ms() == 2000


async def test_end_of_track_is_not_dropped_when_controls_fill_the_queue(
    playback, direct
):
    await load(playback)
    await playback.command("play")
    listener = direct.sessions[0].listener
    for _ in range(32):
        listener.on_command(TransportRequest(TransportKind.RESUME))
    listener.on_finished()
    await drain(playback)
    assert playback.active
    assert playback.state == "STOPPED"


async def test_controller_track_changes_keep_back_and_forward_history(playback, direct):
    first = await load(playback)
    await playback.command("play")
    second = await load(playback, "second")
    third = await load(playback, "third")
    fourth = Media.parse("http://media.test/fourth.flac", "")
    await playback.command("set_next", fourth)
    for expected in (second, first):
        await playback.command("previous")
        assert playback.current == expected
    for expected in (second, third, fourth):
        await playback.command("next")
        assert playback.current == expected
    assert direct.acquire.await_count == 1
    assert playback.next is None


async def test_missing_next_does_not_turn_playback_into_an_error(
    playback, direct, caplog
):
    await load(playback)
    await playback.command("play")
    listener = direct.sessions[0].listener
    with caplog.at_level("INFO"):
        listener.on_command(TransportRequest(TransportKind.NEXT))
        listener.on_command(TransportRequest(TransportKind.PREV))
        await drain(playback)
    assert "has not supplied a next track" in caplog.text
    assert "No previous UPnP track" in caplog.text
    assert playback.status == "OK"
    assert playback.state == "TRANSITIONING"
    assert direct.sessions[0].play.await_count == 1


async def test_next_staged_while_stopped_is_only_queued_on_play(playback, direct):
    await load(playback)
    await playback.command("play")
    await playback.command("stop")
    second = Media.parse("http://media.test/second.flac", "")
    await playback.command("set_next", second)
    hold = direct.sessions[0]
    hold.set_next.assert_not_awaited()
    await playback.command("seek", 5000)
    hold.seek.assert_not_awaited()
    await playback.command("play")
    assert hold.play.call_args.kwargs["start_offset_ms"] == 5000
    hold.set_next.assert_awaited_once_with(second.source, second.track)
    assert direct.acquire.await_count == 1


async def test_next_can_be_replaced_cleared_and_ignored_after_explicit_play(
    playback, direct
):
    await load(playback)
    await playback.command("play")
    second = Media.parse("http://media.test/second.flac", "")
    third = Media.parse("http://media.test/third.flac", "")
    hold = direct.sessions[0]
    for media in (second, third):
        await playback.command("set_next", media)
        hold.set_next.assert_awaited_with(media.source, media.track)
    await playback.command("set_next", None)
    hold.set_next.assert_awaited_with(None)
    current = await load(playback, "replacement")
    hold.listener.on_next_started(second.track)
    assert playback.current == current


async def test_repeated_uri_is_consumed_when_queued_track_starts(playback, direct):
    current = await load(playback)
    await playback.command("play")
    await playback.command("set_next", current)
    direct.sessions[0].listener.on_next_started(current.track)
    await drain(playback)
    assert playback.next is None
    assert playback.previous == current
    assert direct.sessions[0].play.await_count == 1


async def test_history_is_bounded_and_cleared_with_uri(playback):
    for index in range(40):
        await load(playback, str(index))
    assert len(playback.history) == 32
    await playback.command("set_uri", None)
    assert playback.previous is None


async def test_transition_reported_after_new_next_preserves_new_successor(
    playback, direct
):
    await load(playback)
    await playback.command("play")
    second = Media.parse("http://media.test/second.flac", "")
    third = Media.parse("http://media.test/third.flac", "")
    await playback.command("set_next", second)
    await playback.command("set_next", third)
    hold = direct.sessions[0]
    hold.listener.on_next_started(second.track)
    await drain(playback)
    assert playback.current == second
    assert playback.next == third
    assert hold.set_next.await_count == 2
    hold.listener.on_next_started(third.track)
    await drain(playback)
    assert playback.current == third
    assert playback.next is None
