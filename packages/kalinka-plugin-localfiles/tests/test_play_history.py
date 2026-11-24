"""Play history: how much of a library track was heard, and whether it played
out or was skipped."""

import asyncio
import sqlite3

import pytest

from kalinka_plugin_sdk.datamodel import (
    Album,
    EntityId,
    EntityType,
    PlaybackState,
    PlayerStateEnum,
    Track,
)

from kalinka_plugin_localfiles import input_module_db
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.play_history import PlayRecord, PlayTracker, judge


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _track(local, duration=200, source="localfiles"):
    tid = EntityId(id=local, type=EntityType.TRACK, source=source)
    album = EntityId(id="al", type=EntityType.ALBUM, source=source)
    return Track(id=tid, title=local, duration=duration, album=Album(id=album, title=""))


def _state(play_id, track, state=PlayerStateEnum.PLAYING):
    return PlaybackState(play_id=play_id, current_track=track, state=state)


@pytest.mark.parametrize(
    "listened,completed,skipped",
    [(190, True, False), (185, True, False), (100, False, False), (12, False, True)],
)
def test_a_play_is_heard_out_partial_or_skipped(listened, completed, skipped):
    play = judge("t", 1000, 200, listened)

    assert (play.completed, play.skipped) == (completed, skipped)


def test_a_short_track_is_a_skip_only_before_half_of_it():
    assert judge("t", 0, 40, 15).skipped
    assert not judge("t", 0, 40, 25).skipped


def test_a_track_without_a_length_is_never_heard_out():
    assert not judge("t", 0, 0, 500).completed


def test_listening_time_counts_only_while_playing():
    clock = _Clock()
    tracker = PlayTracker("localfiles", clock)
    song = _track("a")

    assert tracker.feed(_state("p1", song)) is None
    clock.now += 60
    tracker.feed(_state("p1", song, PlayerStateEnum.PAUSED))
    clock.now += 600
    tracker.feed(_state("p1", song, PlayerStateEnum.PLAYING))
    clock.now += 130
    finished = tracker.feed(_state("p2", _track("b")))

    assert finished == PlayRecord(
        track_id="a", played_at=1000, listened_s=190, completed=True, skipped=False
    )


def test_a_play_ends_when_nothing_plays():
    clock = _Clock()
    tracker = PlayTracker("localfiles", clock)
    tracker.feed(_state("p1", _track("a")))
    clock.now += 5

    finished = tracker.feed(PlaybackState(state=PlayerStateEnum.STOPPED))

    assert finished.track_id == "a" and finished.skipped


def test_other_sources_tracks_are_not_followed():
    clock = _Clock()
    tracker = PlayTracker("localfiles", clock)
    tracker.feed(_state("p1", _track("q", source="qobuz")))
    clock.now += 200

    assert tracker.feed(_state("p2", _track("a"))) is None


def test_plays_are_kept_newest_last_and_trimmed(tmp_path, monkeypatch):
    cfg = LocalFilesConfig(db_path=str(tmp_path / "h.db"), artwork_path=str(tmp_path / "art"))
    asyncio.run(init_db(cfg.db_path))
    db = LocalFilesInputModuleDb(cfg)
    monkeypatch.setattr(input_module_db, "PLAY_HISTORY_KEPT", 3)

    for i in range(5):
        db.record_play(PlayRecord(f"t{i}", 1000 + i, 100, completed=i % 2 == 0, skipped=False))

    with sqlite3.connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT track_id, completed FROM play_history ORDER BY id").fetchall()
    assert rows == [("t2", 1), ("t3", 0), ("t4", 1)]
