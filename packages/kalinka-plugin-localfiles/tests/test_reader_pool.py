#!/usr/bin/env python3
"""Keeping a track open on a share between the ranges a renderer asks for.

A renderer plays a track as one 384,000-byte range after another, and DSD128
gives each about 270 ms. Probing the share, measuring the file and opening it
for every range spent most of that, so the storage keeps the file open and
the module only pays those trips for a track's first range.
"""

import io
import random
import threading
import time
from typing import BinaryIO, Optional

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.storage import (
    DirEntry,
    FileStat,
    FileStorage,
    RootStatus,
    StorageResolver,
    is_within,
    scheme_of,
)
from kalinka_plugin_localfiles.storage.reader_pool import (
    CAPACITY,
    IDLE_S,
    REMEASURE_S,
    ReaderPool,
)

SCHEME = "share"
ROOT = f"{SCHEME}://nas/music"
TRACK = f"{ROOT}/01 Spem in alium.dsf"
TRACK_ID = "spem"

#: What a renderer asks for at a time.
RANGE_BYTES = 384_000


class _Handle(io.BytesIO):
    """A file open on the share, failing its reads while the share is away
    or its own session has been dropped."""

    def __init__(self, data: bytes, share: "_Share") -> None:
        super().__init__(data)
        self._share = share
        self.was_closed = False
        self.dropped = False
        #: Holds a read until set; ``reading`` says one has begun.
        self.gate: Optional[threading.Event] = None
        self.reading = threading.Event()

    def read(self, size=-1):
        self.reading.set()
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.dropped or self._share.offline:
            raise OSError("nas dropped the connection")
        return super().read(size)

    def close(self):
        self.was_closed = True
        super().close()


class _Share(FileStorage):
    """Files on a share, counting the round trips made for them."""

    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.offline = False
        #: Holds every open until set, as a share that stopped answering.
        self.open_gate: Optional[threading.Event] = None
        self.trips: list[tuple[str, str]] = []
        self.opened: list[_Handle] = []

    @property
    def scheme(self) -> str:
        return SCHEME

    def handles(self, path: str) -> bool:
        return scheme_of(path) == SCHEME

    def canonical(self, path: str) -> str:
        return path

    def contains(self, path: str, roots) -> bool:
        return any(is_within(path, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        return []

    def stat(self, path: str) -> FileStat:
        self.trips.append(("stat", path))
        return FileStat(size=len(self.files[path]), mtime_ns=0, is_dir=False)

    def open(self, path: str, *, read_ahead: bool = True) -> BinaryIO:
        self.trips.append(("open", path))
        if self.open_gate is not None:
            self.open_gate.wait(timeout=5)
        if self.offline:
            raise OSError("nas did not answer")
        handle = _Handle(self.files[path], self)
        self.opened.append(handle)
        return handle

    def probe_root_blocking(self, root: str) -> RootStatus:
        self.trips.append(("probe", root))
        return RootStatus(
            root=root,
            available=True,
            reason="",
            fs_type=SCHEME,
            is_network=True,
            is_autofs=False,
            identity="share nas",
        )


class _Library:
    """The one track the module is asked to serve."""

    def is_good(self) -> bool:
        return True

    def get_track_by_id(self, track_id: str):
        return {"id": TRACK_ID, "file_path": TRACK} if track_id == TRACK_ID else None

    def get_root_signature(self, root: str):
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def audio() -> bytes:
    return random.Random(341).randbytes(10 * RANGE_BYTES + 1234)


@pytest.fixture
def share(audio) -> _Share:
    return _Share({TRACK: audio})


@pytest.fixture
def module(share, tmp_path) -> LocalFilesInputModule:
    resolver = StorageResolver([share])
    config = LocalFilesConfig(
        music_folders=[ROOT],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    made = LocalFilesInputModule(config, _Library(), storage_source=lambda: resolver)
    made._music_folders = [ROOT]
    return made


def _pool(
    share: _Share,
    clock=None,
    scheduled=None,
    capacity: int = CAPACITY,
    reopen_timeout_s: float = 5.0,
) -> ReaderPool:
    """A pool over ``share`` whose time and sweeps the test drives."""
    sweeps = scheduled if scheduled is not None else []
    return ReaderPool(
        lambda path: share.open(path, read_ahead=False),
        reopen_timeout_s=reopen_timeout_s,
        capacity=capacity,
        clock=clock or _Clock(),
        schedule=lambda delay, sweep: sweeps.append((delay, sweep)),
    )


@pytest.fixture
def clock(share) -> _Clock:
    """The time kept by the share's own pool, which the module lends from."""
    moved = _Clock()
    share._readers = _pool(share, moved)
    return moved


def _eventually(condition, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


async def _serve(module: LocalFilesInputModule, start: int, length: int) -> bytes:
    """One range, as the server serves it: resolved, opened, sought — the
    server does not seek for a range at the first byte — read and closed."""
    info = await module.get_content_info(TRACK_ID)
    reader = info.reader()
    try:
        if start:
            reader.seek(start)
        return reader.read(length)
    finally:
        reader.close()


class TestARenderersRanges:
    @pytest.mark.asyncio
    async def test_ten_ranges_cost_one_probe_one_stat_and_one_open(
        self, module, share, audio
    ):
        for start in range(0, 10 * RANGE_BYTES, RANGE_BYTES):
            served = await _serve(module, start, RANGE_BYTES)
            assert served == audio[start:start + RANGE_BYTES]

        assert share.trips == [("probe", ROOT), ("stat", TRACK), ("open", TRACK)]

    @pytest.mark.asyncio
    async def test_the_length_measured_first_is_the_one_every_range_reads(
        self, module, audio
    ):
        await _serve(module, 0, RANGE_BYTES)

        info = await module.get_content_info(TRACK_ID)

        assert info.size == len(audio)
        assert info.local_path is None

    @pytest.mark.asyncio
    async def test_a_kept_reader_starts_the_next_range_at_the_first_byte(
        self, module, audio
    ):
        """The server only seeks for a range that does not start at zero, and
        a kept reader is wherever the last range left it."""
        await _serve(module, 5 * RANGE_BYTES, RANGE_BYTES)

        assert await _serve(module, 0, 100) == audio[:100]

    @pytest.mark.asyncio
    async def test_overlapping_ranges_each_read_their_own_bytes(
        self, module, share, audio
    ):
        """A renderer seeking while a range still streams asks for the new one
        before the old one has finished: sharing the reader would move the
        old one's position under it."""
        streaming = (await module.get_content_info(TRACK_ID)).reader()
        assert streaming.read(1000) == audio[:1000]

        seek = (await module.get_content_info(TRACK_ID)).reader()
        seek.seek(7 * RANGE_BYTES)
        assert seek.read(1000) == audio[7 * RANGE_BYTES:7 * RANGE_BYTES + 1000]
        assert streaming.read(1000) == audio[1000:2000]
        streaming.close()
        seek.close()

        assert len(share.opened) == 2
        assert [handle.was_closed for handle in share.opened] == [False, True]

    @pytest.mark.asyncio
    async def test_a_folder_that_left_the_boundary_is_not_served_from_its_pool(
        self, module
    ):
        """The storage, and the readers it keeps, outlive a re-setup that
        drops the folder, as long as the share logins stay the same."""
        await _serve(module, 0, RANGE_BYTES)
        module._music_folders = [f"{SCHEME}://nas/other"]

        assert await module.get_content_info(TRACK_ID) is None

    def test_a_reader_is_kept_only_once_the_read_on_it_has_finished(
        self, share, audio
    ):
        """A response whose renderer went away is cancelled with its read
        still running on a worker thread, and the server closes the reader
        then. Kept at once, the next range would seek it under that read."""
        pool = share.readers
        abandoned = pool.lend(TRACK, len(audio))
        [handle] = share.opened
        handle.gate = threading.Event()
        reading = threading.Thread(target=abandoned.read, args=(RANGE_BYTES,))
        reading.start()
        assert handle.reading.wait(timeout=5)

        closing = threading.Thread(target=abandoned.close)
        closing.start()
        closing.join(timeout=0.2)
        assert closing.is_alive()

        seek = pool.lend(TRACK, len(audio))
        assert len(share.opened) == 2

        handle.gate.set()
        reading.join(timeout=5)
        closing.join(timeout=5)
        assert not closing.is_alive()
        seek.close()


class TestHowLongAReaderIsKept:
    def test_one_that_no_range_asks_for_is_closed_after_the_idle_period(
        self, share, audio
    ):
        clock, scheduled = _Clock(), []
        pool = _pool(share, clock, scheduled)
        pool.lend(TRACK, len(audio)).close()
        [(delay, sweep)] = scheduled
        assert delay == IDLE_S

        clock.now = IDLE_S - 1
        scheduled.clear()
        sweep()
        assert not share.opened[0].was_closed
        [(delay, sweep)] = scheduled
        assert delay == pytest.approx(1)

        clock.now = IDLE_S
        scheduled.clear()
        sweep()
        assert share.opened[0].was_closed
        assert pool.size_of(TRACK) is None
        assert scheduled == []

    def test_a_range_in_the_meantime_keeps_it_open(self, share, audio):
        clock, scheduled = _Clock(), []
        pool = _pool(share, clock, scheduled)
        pool.lend(TRACK, len(audio)).close()
        clock.now = IDLE_S - 1
        pool.lend(TRACK, len(audio)).close()

        clock.now = IDLE_S
        scheduled.pop(0)[1]()

        assert not share.opened[0].was_closed
        assert pool.size_of(TRACK) == len(audio)

    def test_only_a_handful_are_kept(self, audio):
        tracks = [f"{ROOT}/{n:02d}.dsf" for n in range(CAPACITY + 1)]
        share = _Share({track: audio for track in tracks})
        pool = _pool(share)

        for track in tracks:
            pool.lend(track, len(audio)).close()

        oldest_closed = [True] + [False] * CAPACITY
        assert [handle.was_closed for handle in share.opened] == oldest_closed
        assert pool.size_of(tracks[0]) is None

    def test_a_pool_with_no_room_closes_each_reader_as_it_comes_back(
        self, share, audio
    ):
        pool = _pool(share, capacity=0)

        pool.lend(TRACK, len(audio)).close()

        assert share.opened[0].was_closed
        assert pool.size_of(TRACK) is None

    def test_closing_the_storage_closes_what_it_keeps(self, share, audio):
        """A storage is closed when the credentials it signed in with change,
        and a file it kept open would go on being read as the old account."""
        share.readers.lend(TRACK, len(audio)).close()

        share.close()

        assert share.opened[0].was_closed
        assert share.readers.size_of(TRACK) is None
        share.readers.lend(TRACK, len(audio)).close()
        assert share.opened[1].was_closed

    def test_one_lent_out_is_closed_when_it_comes_back(self, share, audio):
        streaming = share.readers.lend(TRACK, len(audio))

        share.close()
        assert not share.opened[0].was_closed
        streaming.close()

        assert share.opened[0].was_closed


class TestAShareThatDrops:
    @pytest.mark.asyncio
    async def test_a_dropped_session_is_read_through_on_the_file_opened_afresh(
        self, module, share, audio
    ):
        """A kept reader can outlive the session it was opened on, and the
        share still has the bytes."""
        await _serve(module, 0, RANGE_BYTES)
        share.opened[0].dropped = True

        served = await _serve(module, RANGE_BYTES, RANGE_BYTES)

        assert served == audio[RANGE_BYTES:2 * RANGE_BYTES]
        assert share.opened[0].was_closed
        assert [trip for trip, _ in share.trips] == ["probe", "stat", "open", "open"]

    @pytest.mark.asyncio
    async def test_a_share_that_stays_away_is_checked_again_by_the_next_range(
        self, module, share, audio
    ):
        await _serve(module, 0, RANGE_BYTES)
        share.offline = True

        with pytest.raises(OSError):
            await _serve(module, RANGE_BYTES, RANGE_BYTES)
        assert share.opened[0].was_closed

        share.offline = False
        share.trips.clear()
        served = await _serve(module, RANGE_BYTES, RANGE_BYTES)

        assert served == audio[RANGE_BYTES:2 * RANGE_BYTES]
        assert share.trips == [("probe", ROOT), ("stat", TRACK), ("open", TRACK)]

    def test_a_reader_opened_for_the_range_gets_no_second_try(self, share, audio):
        """It cannot have outlived its session, so a failed read says the
        share is failing now."""
        pool = _pool(share)
        lease = pool.lend(TRACK, len(audio))
        share.opened[0].dropped = True

        with pytest.raises(OSError):
            lease.read(100)
        lease.close()

        assert len(share.opened) == 1
        assert share.opened[0].was_closed
        assert pool.size_of(TRACK) is None

    def test_opening_again_is_given_up_in_time(self, share, audio):
        """A share that stopped answering has already held this response
        once, through the read that failed."""
        pool = _pool(share, reopen_timeout_s=0.05)
        pool.lend(TRACK, len(audio)).close()
        share.opened[0].dropped = True
        share.open_gate = threading.Event()
        lease = pool.lend(TRACK, len(audio))

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            lease.read(100)
        assert time.monotonic() - started < 2.0
        lease.close()
        assert pool.size_of(TRACK) is None

        share.open_gate.set()
        assert _eventually(
            lambda: len(share.opened) == 2 and share.opened[1].was_closed
        )


class TestHowLongALengthIsTrusted:
    def test_a_busy_track_is_measured_again_after_a_while(self, share, audio):
        """Every range resets the idle clock, so activity alone would vouch
        for the first length for as long as the track is played."""
        clock = _Clock()
        pool = _pool(share, clock)
        pool.lend(TRACK, len(audio)).close()
        for second in range(10, int(REMEASURE_S), 10):
            clock.now = second
            assert pool.size_of(TRACK) == len(audio)
            pool.lend(TRACK, len(audio)).close()

        clock.now = REMEASURE_S
        assert pool.size_of(TRACK) is None

        pool.lend(TRACK, len(audio)).close()
        assert pool.size_of(TRACK) == len(audio)
        assert len(share.opened) == 1

    def test_a_new_length_closes_the_reader_opened_on_the_old_one(
        self, share, audio
    ):
        pool = _pool(share)
        pool.lend(TRACK, len(audio)).close()
        share.files[TRACK] = audio[:-1000]

        pool.lend(TRACK, len(audio) - 1000).close()

        assert len(share.opened) == 2
        assert share.opened[0].was_closed
        assert not share.opened[1].was_closed
        assert pool.size_of(TRACK) == len(audio) - 1000

    def test_a_length_from_the_pool_does_not_outlive_a_forgotten_file(
        self, share, audio
    ):
        """A range that failed between another's size_of and its lend leaves
        the share to be checked by the next one, not vouched for anew."""
        pool = _pool(share)
        pool.lend(TRACK, len(audio)).close()
        size = pool.size_of(TRACK)
        failing = pool.lend(TRACK, len(audio))
        share.offline = True
        with pytest.raises(OSError):
            failing.read(100)
        failing.close()
        share.offline = False

        late = pool.lend(TRACK, size, measured=False)
        assert late.read(100) == audio[:100]
        late.close()

        assert pool.size_of(TRACK) is None
        assert share.opened[-1].was_closed

    @pytest.mark.asyncio
    async def test_a_file_replaced_while_it_plays_is_served_anew(
        self, module, share, audio, clock
    ):
        """Re-encoded in place by a tagger or a transcoder, it is checked,
        measured and opened again within minutes, however busy it is."""
        await _serve(module, 0, RANGE_BYTES)
        clock.now = REMEASURE_S - 1
        await _serve(module, RANGE_BYTES, RANGE_BYTES)
        replaced = audio[::-1][: 5 * RANGE_BYTES]
        share.files[TRACK] = replaced

        clock.now = REMEASURE_S
        served = await _serve(module, 0, RANGE_BYTES)

        assert served == replaced[:RANGE_BYTES]
        assert [trip for trip, _ in share.trips] == [
            "probe",
            "stat",
            "open",
            "probe",
            "stat",
            "open",
        ]
        assert share.opened[0].was_closed
        assert (await module.get_content_info(TRACK_ID)).size == len(replaced)
