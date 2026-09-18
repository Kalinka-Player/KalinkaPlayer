#!/usr/bin/env python3
"""
Tests for the indexer's broken-file handling (issue #63):

  1. An MP3 with no ID3 header is a valid file and must still be indexed.
  2. A file whose metadata can't be extracted is parked in a negative
     cache and not re-read on every scan...
  3. ...unless it changes (size/mtime), e.g. a still-uploading file, in
     which case it is retried.
  4. A later successful index clears the failure record.
"""

import io
import os

import pytest
import pytest_asyncio


from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer import indexer as indexer_mod
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


@pytest_asyncio.fixture
async def indexer(tmp_path):
    """A FileIndexer backed by a real (temporary) sqlite database."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music_dir)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,  # skip the upload-quiescence sleep in tests
    )
    await init_db(config.db_path)
    db = AsyncIndexerDb(config)
    return FileIndexer(config, db), music_dir


def _write(path, data=b"x"):
    path.write_bytes(data)
    return str(path)


@pytest.mark.asyncio
async def test_failed_file_is_parked_and_not_reread(indexer, monkeypatch):
    fi, music_dir = indexer
    file_path = _write(music_dir / "broken.mp3")

    calls = {"n": 0}

    def fake_extract(_storage, _path):
        calls["n"] += 1
        return None  # simulate extraction failure

    monkeypatch.setattr(fi, "_extract_metadata", fake_extract)

    # First pass: extraction runs, fails, and is recorded.
    assert await fi.process_file(file_path) is None
    assert calls["n"] == 1
    failure = await fi.db_manager.get_failure(file_path)
    assert failure is not None
    assert failure["attempts"] == 1

    # Second pass (file unchanged): served from the negative cache, the
    # extractor is NOT invoked again — this is the "retry forever" fix.
    assert await fi.process_file(file_path) is None
    assert calls["n"] == 1
    assert (await fi.db_manager.get_failure(file_path))["attempts"] == 1


@pytest.mark.asyncio
async def test_changed_file_is_retried(indexer, monkeypatch):
    fi, music_dir = indexer
    p = music_dir / "uploading.mp3"
    file_path = _write(p, b"partial")

    calls = {"n": 0}
    monkeypatch.setattr(
        fi,
        "_extract_metadata",
        lambda _s, _p: (calls.__setitem__("n", calls["n"] + 1), None)[1],
    )

    assert await fi.process_file(file_path) is None
    assert calls["n"] == 1

    # File grows (still uploading) -> different size key -> retried, and the
    # attempt counter resets rather than climbing.
    _write(p, b"partial-but-bigger-now")
    assert await fi.process_file(file_path) is None
    assert calls["n"] == 2
    assert (await fi.db_manager.get_failure(file_path))["attempts"] == 1


@pytest.mark.asyncio
async def test_success_clears_failure(indexer, monkeypatch):
    fi, music_dir = indexer
    p = music_dir / "fixed.mp3"
    file_path = _write(p, b"partial")

    state = {"ok": False}

    def fake_extract(_storage, _path):
        if not state["ok"]:
            return None
        return {
            "format": "audio/mpeg",
            "duration": 123,
            "title": "Fixed",
            "artist": "Some Artist",
            "album": "Some Album",
        }

    monkeypatch.setattr(fi, "_extract_metadata", fake_extract)

    assert await fi.process_file(file_path) is None
    assert await fi.db_manager.get_failure(file_path) is not None

    # Upload completes: bump size so the cache key changes, then succeed.
    state["ok"] = True
    _write(p, b"complete-file-contents")
    changes = await fi.process_file(file_path)
    assert changes is not None and changes["tracks"]
    assert await fi.db_manager.get_failure(file_path) is None
    assert await fi.db_manager.get_track_by_path(file_path) is not None


@pytest.mark.asyncio
async def test_cleanup_prunes_failures_for_deleted_files(indexer, monkeypatch):
    fi, music_dir = indexer
    p = music_dir / "gone.mp3"
    file_path = _write(p)
    monkeypatch.setattr(fi, "_extract_metadata", lambda _s, _p: None)

    await fi.process_file(file_path)
    assert await fi.db_manager.get_failure(file_path) is not None

    # A sibling keeps the root alive: an empty unsigned root reads as an
    # unmounted share and blocks the cleanup.
    _write(music_dir / "stays.mp3")
    os.remove(file_path)
    await fi.cleanup_stale_tracks()
    assert await fi.db_manager.get_failure(file_path) is None


@pytest.mark.asyncio
async def test_subsecond_mtime_change_is_retried(indexer, monkeypatch):
    """A fixed-but-broken file re-written within the same integer second,
    keeping the same size, must still be retried. The failure-cache key uses
    nanosecond mtime (stat.st_mtime_ns), not truncated seconds, so it doesn't
    collide on the second boundary (PR #67 review)."""
    fi, music_dir = indexer
    p = music_dir / "samesize.mp3"
    file_path = _write(p, b"aaaaaaaa")  # 8 bytes

    base_s = 1_700_000_000
    os.utime(file_path, ns=(base_s * 10**9, base_s * 10**9 + 100_000_000))
    if os.stat(file_path).st_mtime_ns % 10**9 == 0:
        pytest.skip("filesystem lacks sub-second mtime resolution")

    calls = {"n": 0}
    monkeypatch.setattr(
        fi, "_extract_metadata",
        lambda _s, _p: (calls.__setitem__("n", calls["n"] + 1), None)[1],
    )

    # First pass: fails and is recorded with the +0.10s nanosecond mtime.
    assert await fi.process_file(file_path) is None
    assert calls["n"] == 1

    # Same byte length, same integer second, different nanoseconds (+0.90s).
    # Second-resolution keys would treat this as "unchanged" and skip it;
    # the nanosecond key sees a change and retries.
    p.write_bytes(b"bbbbbbbb")  # still 8 bytes
    os.utime(file_path, ns=(base_s * 10**9, base_s * 10**9 + 900_000_000))
    assert await fi.process_file(file_path) is None
    assert calls["n"] == 2


def test_mp3_without_id3_header_is_supported(monkeypatch):
    """A tagless MP3 must extract (duration only), not raise.

    ``MP3`` reports a missing header as ``tags = None``, having already read
    the frames while opening the file — which is why nothing re-reads them.
    """
    fi = FileIndexer.__new__(FileIndexer)  # no DB needed for this unit

    class FakeInfo:
        length = 200.0

    class FakeMP3:
        def __init__(self, _path):
            self.info = FakeInfo()
            self.tags = None

    monkeypatch.setattr(indexer_mod, "MP3", FakeMP3)
    monkeypatch.setattr(indexer_mod, "ID3", dict)

    metadata = fi._extract_mp3_metadata(
        io.BytesIO(b"no tags here"), {"format": "audio/mpeg"}
    )
    assert metadata["duration"] == 200
    assert "title" not in metadata
    assert "artist" not in metadata


def test_an_mp3_is_read_once(monkeypatch):
    """Tags come off the object ``MP3`` already built. Re-parsing them meant
    a second pass over the file, which on a share is a second network read
    of a buffer that was just discarded."""
    fi = FileIndexer.__new__(FileIndexer)
    reads = {"n": 0}

    class FakeInfo:
        length = 90.0

    class FakeMP3:
        def __init__(self, _stream):
            reads["n"] += 1
            self.info = FakeInfo()
            self.tags = {"TIT2": "Come Together"}

    def refuse(*args):
        raise AssertionError("the frames were parsed a second time")

    monkeypatch.setattr(indexer_mod, "MP3", FakeMP3)
    monkeypatch.setattr(indexer_mod, "ID3", refuse)

    metadata = fi._extract_mp3_metadata(
        io.BytesIO(b"tagged"), {"format": "audio/mpeg"}
    )
    assert reads["n"] == 1
    assert metadata["title"] == "Come Together"
