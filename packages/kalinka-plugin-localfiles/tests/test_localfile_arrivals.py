"""Loose numbered rips: copying, naming, fingerprint rescue and recency, and
how an arriving file's rows are written."""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from art_images import patterned_png
from kalinka_plugin_localfiles import db_connection
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_connection import shared_connection
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.acoustid_plugin import AcoustIdPlugin
from kalinka_plugin_localfiles.enricher.enricher import MetadataEnricher
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.filename_model import parse_music_path
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


@pytest_asyncio.fixture
async def library(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "art"),
        quiescence_seconds=0,
        folder_first_clustering=True,
    )
    await init_db(config.db_path)
    return FileIndexer(config, AsyncIndexerDb(config)), music, config


def _write(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    if tags:
        audio = FLAC(path)
        audio.update(tags)
        audio.save()
    return str(path)


@pytest.mark.parametrize("name", ["track-02.flac", "track-03.flac", "track-04.flac"])
def test_numbered_placeholder_does_not_name_an_artist(name):
    parsed = parse_music_path(f"/mnt/usb/Music/{name}", "/mnt/usb/Music")
    assert parsed.artist is None
    assert parsed.album is None
    assert parsed.track_number == int(name[6:8])


@pytest.mark.asyncio
async def test_recluster_dissolves_old_music_album_and_restores_recency(library):
    fi, music, config = library
    ids = []
    for name in ("The Heat.flac", "track-02.flac", "track-03.flac", "track-04.flac"):
        ids.append((await fi.process_file(_write(music / name)))["tracks"])
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            "INSERT INTO albums (id,title,artist_id) VALUES ('old','Music','unknown_artist')"
        )
        conn.execute("UPDATE tracks SET album_id='old'")
        for stamp, tid in enumerate(ids, 1):
            conn.execute(
                "UPDATE library_file SET first_indexed=? WHERE file_id=?", (stamp, tid)
            )

    await fi.recluster()

    with sqlite3.connect(config.db_path) as conn:
        assert conn.execute("SELECT DISTINCT album_id FROM tracks").fetchall() == [
            ("unknown_album",)
        ]
        assert not conn.execute("SELECT id FROM albums WHERE id='old'").fetchall()
    recent, total = LocalFilesInputModuleDb(config).list_recently_added()
    assert total == 4
    assert [(kind, row["id"]) for kind, row in recent] == [
        ("track", tid) for tid in reversed(ids)
    ]
    assert (await fi.recluster())["reassigned"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["Music", "Downloads", "My collection"])
async def test_container_preserves_explicit_album_without_absorbing_loose_files(
    library, folder
):
    fi, music, config = library
    root = music if folder == "Music" else music / folder
    if folder == "My collection":
        root.mkdir()
        fi.music_folders = [str(root)]
    tagged = (
        await fi.process_file(_write(root / "01.flac", artist="Madonna", album="Music"))
    )["tracks"]
    loose = (await fi.process_file(_write(root / "track-02.flac")))["tracks"]
    await fi.recluster()
    with sqlite3.connect(config.db_path) as conn:
        assert (
            conn.execute("SELECT album_id FROM tracks WHERE id=?", (loose,)).fetchone()[
                0
            ]
            == "unknown_album"
        )
        assert (
            conn.execute(
                "SELECT a.title FROM tracks t JOIN albums a ON a.id=t.album_id WHERE t.id=?",
                (tagged,),
            ).fetchone()[0]
            == "Music"
        )


@pytest.mark.asyncio
async def test_container_keeps_tagged_disc_variants_in_one_album(library):
    fi, music, config = library
    for disc in (1, 2):
        await fi.process_file(
            _write(
                music / f"disc-{disc}.flac",
                artist="Artist",
                albumartist="Artist",
                album=f"Double Album (Disc {disc})",
                discnumber=str(disc),
                tracknumber="1",
            )
        )
    await fi.recluster()
    with sqlite3.connect(config.db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.id,a.title FROM tracks t JOIN albums a ON a.id=t.album_id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "Double Album"


@pytest.mark.asyncio
async def test_incremental_arrival_clusters_an_album_without_waiting_for_full_scan(
    library,
):
    fi, music, config = library
    paths = [_write(music / "TFX" / f"track-0{n}.flac") for n in (2, 3)]
    await fi.handle_incremental_changes({("file_closed", p) for p in paths})
    with sqlite3.connect(config.db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.title FROM tracks t JOIN albums a ON a.id=t.album_id"
        ).fetchall()
        assert rows == [("TFX",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["The Mix", "VA - Summer Mix"])
async def test_untagged_named_mix_is_still_an_album(library, folder):
    fi, music, config = library
    paths = [_write(music / folder / f"track-0{n}.flac") for n in (2, 3)]
    await fi.handle_incremental_changes({("file_closed", p) for p in paths})
    with sqlite3.connect(config.db_path) as conn:
        albums = conn.execute("SELECT DISTINCT album_id FROM tracks").fetchall()
        assert len(albums) == 1
        assert albums[0][0] != "unknown_album"


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_root", [False, True])
async def test_timestamp_restored_after_close_is_retried_without_another_event(
    library, monkeypatch, remove_root
):
    fi, music, config = library
    config.quiescence_seconds = 1
    path = _write(music / "track-03.flac")

    # Reproduce cp/scp restoring an old source mtime after CLOSE_WRITE.
    async def restore_timestamp(delay):
        stamp = time.time() - 120
        os.utime(path, (stamp, stamp))

    with patch(
        "kalinka_plugin_localfiles.indexer.indexer.asyncio.sleep", restore_timestamp
    ):
        await fi.handle_incremental_changes({("file_closed", path)})
    assert await fi.db_manager.get_track_by_path(path) is None
    assert 0 < fi.deferred_retry_in() <= 1
    await fi.retry_deferred_files()
    assert await fi.db_manager.get_track_by_path(path) is None
    deadline = time.monotonic() + 2
    monkeypatch.setattr(
        "kalinka_plugin_localfiles.indexer.indexer.time.monotonic", lambda: deadline
    )
    if remove_root:
        fi.music_folders = []
    await fi.retry_deferred_files()
    if remove_root:
        assert await fi.db_manager.get_track_by_path(path) is None
        assert fi.deferred_retry_in() == 30
        return
    assert (await fi.db_manager.get_track_by_path(path))["album_id"] == "unknown_album"
    assert fi.deferred_retry_in() == 30


@pytest.mark.asyncio
async def test_quiescence_window_counts_from_the_exact_mtime(library, monkeypatch):
    from kalinka_plugin_localfiles.indexer import indexer as module

    fi, music, config = library
    config.quiescence_seconds = 1
    path = _write(music / "track-03.flac")
    written_ns = 1_800_000_000_900_000_000
    os.utime(path, ns=(written_ns, written_ns))
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(time=lambda: written_ns / 1e9 + 0.2, monotonic=time.monotonic),
    )
    waits = []

    async def wait(delay):
        waits.append(delay)

    with patch("kalinka_plugin_localfiles.indexer.indexer.asyncio.sleep", wait):
        await fi.handle_incremental_changes({("file_closed", path)})
    assert waits == [pytest.approx(0.8, abs=1e-3)]
    assert await fi.db_manager.get_track_by_path(path) is not None


_FILE_ROWS = (
    "tracks",
    "library_file",
    "track_evidence",
    "resolved_origin",
    "artists",
    "albums",
)
_STORAGE_ACCESS = (
    "listdir",
    "stat",
    "readable",
    "open",
    "exists",
    "is_gone",
    "is_dir",
    "is_file",
)


def _new_album_track(music):
    """A file of an album and artist the library does not have yet, with a
    year and a cover: every row a file can bring is written for it."""
    path = _write(
        music / "Artist" / "Album" / "01.flac",
        artist="Artist",
        album="Album",
        title="One",
        date="2001",
    )
    audio = FLAC(path)
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/png"
    picture.data = patterned_png(7)
    audio.add_picture(picture)
    audio.save()
    return path


def _trace(monkeypatch, events):
    """Record in ``events`` every statement a library connection runs."""
    connected = db_connection._connected

    @asynccontextmanager
    async def traced(db_path):
        async with connected(db_path) as conn:
            await conn.set_trace_callback(events.append)
            yield conn

    monkeypatch.setattr(db_connection, "_connected", traced)


def _row_counts(config):
    with closing(sqlite3.connect(config.db_path)) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _FILE_ROWS
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("inside_a_scan", [True, False])
async def test_a_new_file_is_written_in_one_commit(
    library, monkeypatch, inside_a_scan
):
    """Each commit is a sync to disk, and this file once took twelve."""
    fi, music, config = library
    path = _new_album_track(music)
    statements = []
    _trace(monkeypatch, statements)
    async with shared_connection(config.db_path) if inside_a_scan else nullcontext():
        changes = await fi.process_file(path)
    assert all(changes.values())
    assert statements.count("COMMIT") == 1


@pytest.mark.asyncio
async def test_a_new_file_is_read_and_its_cover_stored_before_its_rows_are_written(
    library, monkeypatch
):
    """The transaction keeps every other writer of the library waiting, so
    no slow share or large cover may keep it open."""
    fi, music, config = library
    path = _new_album_track(music)
    events = []
    _trace(monkeypatch, events)

    def recorded(name, fn):
        def call(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                events.append(name)

        return call

    storage = fi.storage.for_path(path)
    for name in _STORAGE_ACCESS:
        monkeypatch.setattr(storage, name, recorded(name, getattr(storage, name)))
    monkeypatch.setattr(fi, "_extract_metadata", recorded("tags", fi._extract_metadata))
    monkeypatch.setattr(fi, "_save_images", recorded("cover", fi._save_images))

    await fi.process_file(path)

    begin, commit = events.index("BEGIN IMMEDIATE"), events.index("COMMIT")
    assert {"stat", "open", "tags", "cover"} <= set(events[:begin])
    assert not {*_STORAGE_ACCESS, "tags", "cover"} & set(events[begin:commit])


@pytest.mark.asyncio
async def test_a_file_that_fails_half_way_through_its_rows_leaves_none(
    library, monkeypatch
):
    """A crash between two of a track's commits once left a tracks row
    without its library_file or track_evidence, which no scan repaired."""
    fi, music, config = library
    path = _new_album_track(music)
    before = _row_counts(config)
    insert_track = fi.db_manager.insert_track

    async def insert_then_fail(data):
        await insert_track(data)
        raise RuntimeError("the power went")

    monkeypatch.setattr(fi.db_manager, "insert_track", insert_then_fail)
    with pytest.raises(RuntimeError, match="power"):
        await fi.process_file(path)
    assert _row_counts(config) == before

    monkeypatch.undo()
    track_id = (await fi.process_file(path))["tracks"]
    with closing(sqlite3.connect(config.db_path)) as conn:
        for table, key in (("library_file", "file_id"), ("track_evidence", "track_id")):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {key} = ?", (track_id,)
            ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_rows_another_writer_adds_before_the_transaction_are_kept(
    library, monkeypatch
):
    """The artist and album are looked up before the cover is stored, and
    the enricher can add the artist meanwhile; INSERT OR REPLACE would
    wipe what it wrote."""
    fi, music, config = library
    path = _new_album_track(music)
    album_for = fi._album_for
    added = {}

    async def then_another_writer(*args):
        album = await album_for(*args)
        assert album.new_row is not None
        added.update(artist=args[3], album=album.id)
        with closing(sqlite3.connect(config.db_path)) as conn:
            conn.execute(
                "INSERT INTO artists (id, name, mbid) VALUES (?, 'Artist', 'mb-1')",
                (args[3],),
            )
            conn.execute(
                "INSERT INTO albums (id, title, artist_id) VALUES (?, 'Kept', ?)",
                (album.id, args[3]),
            )
            conn.commit()
        return album

    monkeypatch.setattr(fi, "_album_for", then_another_writer)
    changes = await fi.process_file(path)

    assert changes["artists"] is None and changes["albums"] is None
    with closing(sqlite3.connect(config.db_path)) as conn:
        assert conn.execute(
            "SELECT mbid FROM artists WHERE id = ?", (added["artist"],)
        ).fetchone() == ("mb-1",)
        assert conn.execute(
            "SELECT title FROM albums WHERE id = ?", (added["album"],)
        ).fetchone() == ("Kept",)


DAY = 24 * 60 * 60
NEW_FILE = {"incremental_changes": {("file_closed", "/music/new.flac")}}


@pytest.fixture
def worker(monkeypatch):
    """`_indexer_worker` over a fake indexer, queue and clock.

    Applying a change takes the fake indexer a day, which is how a test lets
    time pass between the commands it queues.
    """
    from kalinka_plugin_localfiles.indexer import indexer as module

    run = SimpleNamespace(now=0, hour=4, scans=[], changes=[], retries=0)
    queue = asyncio.Queue()
    shutdown = asyncio.Event()

    class Indexer:
        storage = SimpleNamespace(close=lambda: None)

        async def start(self):
            run.scans.append(run.now)
            # Back-to-back scans never yield, so end the run instead of hanging.
            if len(run.scans) > 3:
                shutdown.set()
            return True

        async def handle_incremental_changes(self, changes):
            run.changes.append(changes)
            run.now += DAY

        def deferred_retry_in(self):
            return 30

        async def retry_deferred_files(self):
            run.retries += 1

    async def until_stopped(*commands, daily):
        for command in (*commands, "stop"):
            queue.put_nowait(command)
        await module._indexer_worker(LocalFilesConfig(daily_rescan=daily), None)

    monkeypatch.setattr(module, "FileIndexer", lambda *args: Indexer())
    monkeypatch.setattr(module, "_indexer_queue", queue)
    monkeypatch.setattr(module, "_shutdown_event", shutdown)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: run.now,
            localtime=lambda: SimpleNamespace(tm_hour=run.hour),
        ),
    )
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    run.until_stopped = until_stopped
    return run


@pytest.mark.asyncio
async def test_incremental_events_do_not_postpone_the_nightly_scan(worker):
    await worker.until_stopped(NEW_FILE, daily=True)
    assert worker.scans == [0, DAY]


@pytest.mark.asyncio
async def test_the_nightly_scan_waits_for_the_small_hours(worker):
    worker.hour = 14
    await worker.until_stopped(NEW_FILE, daily=True)
    assert worker.scans == [0]


@pytest.mark.asyncio
async def test_without_the_daily_rescan_it_scans_only_when_asked(worker):
    await worker.until_stopped(NEW_FILE, "scan", daily=False)
    assert worker.scans == [0, DAY]


@pytest.mark.asyncio
async def test_without_the_daily_rescan_changes_and_retries_still_apply(worker):
    """Scanning back to back once kept the worker from reading its queue."""
    await worker.until_stopped(NEW_FILE, daily=False)
    assert worker.changes == [NEW_FILE["incremental_changes"]]
    assert worker.retries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("local_artist", [None, "Track"])
async def test_artist_rescue_uses_file_evidence_after_text_match(library, local_artist):
    fi, music, config = library
    tags = {"artist": local_artist} if local_artist else {}
    tid = (await fi.process_file(_write(music / "track-02.flac", **tags)))["tracks"]
    db = AsyncEnricherDb(config)
    await fi.db_manager.insert_artist(
        {"id": "wrong", "name": "Track", "mbid": "wrong-mbid"}
    )
    await fi.db_manager.update_track(tid, {"artist_id": "wrong"})
    await db.record_resolved_origin(
        "artist", "wrong", "name", "musicbrainz:wrong-mbid", "inferred"
    )
    track = await db.get_track_by_id(tid)
    config.enricher.plugins.acoustid.api_key = "test"
    plugin = AcoustIdPlugin(config, db)
    match = {
        "score": 0.99,
        "recording_mbid": "recording",
        "title": "Introduction",
        "artist_name": "Barry Leitch",
        "artist_mbid": "barry",
    }
    with (
        patch.object(plugin, "_generate_fingerprint", return_value=("fp", 293)),
        patch.object(plugin, "_lookup_fingerprint", return_value=[{}]),
        patch.object(plugin, "_get_best_match_info", return_value=match),
    ):
        result = await plugin.enrich_track(track)
    if local_artist:
        assert "artist_id" not in result["updates"]
    else:
        assert result["updates"]["artist_name"] == "Barry Leitch"
        assert (await db.get_track_artist_origin(tid))["tier"] == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [{}, {"artist": ["Track"]}, None])
async def test_upgrade_retries_only_legacy_fingerprint_matches_with_untagged_artist(
    library, evidence
):
    fi, music, config = library
    tid = (await fi.process_file(_write(music / "track-02.flac")))["tracks"]
    db = AsyncEnricherDb(config)
    await fi.db_manager.insert_artist(
        {"id": "wrong", "name": "Track", "mbid": "wrong-mbid"}
    )
    await fi.db_manager.update_track(
        tid,
        {
            "title": "Introduction",
            "artist_id": "wrong",
            "mbid": "recording",
            "enriched": 1,
        },
    )
    await db.record_resolved_origin(
        "artist", "wrong", "name", "musicbrainz:wrong-mbid", "inferred"
    )
    await db.record_resolved_origin(
        "track", tid, "title", "acoustid:recording", "verified"
    )
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            "DELETE FROM resolved_origin WHERE entity_type='track' AND entity_id=? AND field='artist_id'",
            (tid,),
        )
        conn.execute(
            "UPDATE track_evidence SET raw_tags=? WHERE track_id=?",
            (json.dumps(evidence) if evidence is not None else None, tid),
        )
    origin = await db.get_track_artist_origin(tid)
    if evidence == {}:
        assert origin["tier"] == "guessed"
    elif evidence:
        assert origin["tier"] == "observed"
    else:
        assert origin is None
    await db.reset_failed_for_fingerprint("new-matcher")
    assert (await db.get_track_by_id(tid))["enriched"] == (0 if evidence == {} else 1)
    assert await db.reset_failed_for_fingerprint("new-matcher") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("already_identified", [False, True])
async def test_complete_fields_do_not_skip_fingerprint_artist_rescue(
    library, already_identified
):
    fi, music, config = library
    tid = (await fi.process_file(_write(music / "track-02.flac")))["tracks"]
    db = AsyncEnricherDb(config)
    await fi.db_manager.insert_artist(
        {"id": "wrong", "name": "Track", "mbid": "wrong-mbid"}
    )
    await fi.db_manager.update_track(
        tid,
        {
            "artist_id": "wrong",
            "duration": 293,
            "mbid": "recording" if already_identified else None,
        },
    )
    await db.record_resolved_origin(
        "artist", "wrong", "name", "musicbrainz:wrong-mbid", "inferred"
    )
    config.enricher.plugins.acoustid.api_key = "test"
    plugin = AcoustIdPlugin(config, db)
    plugin.fpcalc = "fpcalc"

    class TextMatch:
        runs_after_resolution = False

        def can_enrich_track(self):
            return True

        async def enrich_track(self, track):
            return {"updates": {"mbid": "text-search-recording"}}

    enricher = MetadataEnricher(config, db)
    enricher.plugins = [TextMatch(), plugin]
    match = {
        "score": 0.99,
        "recording_mbid": "recording",
        "title": "Introduction",
        "artist_name": "Barry Leitch",
        "artist_mbid": "barry",
    }
    with (
        patch.object(plugin, "_generate_fingerprint", return_value=("fp", 293)),
        patch.object(plugin, "_lookup_fingerprint", return_value=[{}]),
        patch.object(plugin, "_get_best_match_info", return_value=match),
    ):
        await enricher._enrich_track(await db.get_track_by_id(tid))
    row = await db.get_track_by_id(tid)
    assert (row["title"], row["artist_name"], row["mbid"], row["enriched"]) == (
        "Introduction",
        "Barry Leitch",
        "recording",
        1,
    )
