"""The library's connections are kept per thread and hold nothing between
calls: no lock another connection waits on, and no stale view."""

import asyncio
import sqlite3
import threading

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


def _library(path):
    asyncio.run(init_db(str(path)))
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO artists (id, name) VALUES (?, ?)",
            [("a1", "One"), ("a2", "Two")],
        )
    conn.close()


@pytest.fixture
def db(tmp_path):
    config = LocalFilesConfig(
        db_path=str(tmp_path / "library.db"), artwork_path=str(tmp_path / "artwork")
    )
    _library(config.db_path)
    return LocalFilesInputModuleDb(config)


@pytest.fixture
def opened(monkeypatch):
    """The thread of every connection the library opens."""
    threads = []
    connect = sqlite3.connect

    def counting(*args, **kwargs):
        threads.append(threading.get_ident())
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    return threads


def _other(db):
    """A connection of another party's that fails at once on a lock."""
    return sqlite3.connect(db.db_path, timeout=0, isolation_level=None)


def _artists(db):
    conn = db._get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    finally:
        conn.close()


def test_a_thread_opens_one_connection_however_many_calls_it_makes(db, opened):
    for _ in range(3):
        db.get_track_by_id("missing")
    assert len(opened) == 1


def test_each_thread_has_a_connection_of_its_own(db, opened):
    db.get_track_by_id("missing")
    errors = []

    def elsewhere():
        try:
            db.get_track_by_id("missing")
        except sqlite3.Error as e:
            errors.append(e)

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()
    assert errors == []
    assert len(set(opened)) == 2


def test_a_query_left_half_read_holds_no_checkpoint_back(db):
    conn = db._get_connection()
    cursor = conn.execute("SELECT * FROM artists")
    cursor.fetchone()
    conn.close()

    other = _other(db)
    other.execute("INSERT INTO artists (id, name) VALUES ('a3', 'Three')")
    busy, _, _ = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    other.close()
    assert busy == 0, "a reader still held the WAL"


def test_a_write_left_uncommitted_is_rolled_back_and_locks_nothing(db):
    before = _artists(db)
    conn = db._get_connection()
    conn.execute("INSERT INTO artists (id, name) VALUES ('a3', 'Three')")
    conn.close()

    other = _other(db)
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()
    assert _artists(db) == before


def test_the_next_call_reads_what_another_connection_wrote(db):
    before = _artists(db)
    other = _other(db)
    other.execute("INSERT INTO artists (id, name) VALUES ('a3', 'Three')")
    other.close()
    assert _artists(db) == before + 1


def test_a_call_made_while_the_connection_is_out_gets_one_of_its_own(db):
    before = _artists(db)
    outer = db._get_connection()
    inner = db._get_connection()
    assert inner is not outer
    inner.execute("SELECT 1")
    inner.close()
    with pytest.raises(sqlite3.ProgrammingError):
        inner.execute("SELECT 1")
    assert outer.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == before
    outer.close()


def test_a_replaced_database_file_is_opened_afresh(db):
    assert _artists(db) > 1, "the seeded artists"
    db._purge_database()
    asyncio.run(init_db(str(db.db_path)))
    assert _artists(db) == 1, "only the new file's own unknown artist"
