"""One connection per library-database call, or one for a whole scan.

Opening a connection costs about a millisecond, and indexing a new file
makes more than a dozen calls, so a scan shares one. What sharing must not
change is what a call could rely on with a connection of its own: its own
transaction, rows as plain tuples, and no hold on the database between
calls, which other processes write too.
"""

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from kalinka_plugin_localfiles.db_connection import LibraryDb, shared_connection


class _Notes(LibraryDb):
    def __init__(self, db_path):
        self.db_path = db_path

    async def add(self, text, commit=True):
        async with self._open() as conn:
            await conn.execute("INSERT INTO notes (text) VALUES (?)", (text,))
            if commit:
                await conn.commit()

    async def add_then_fail(self, text):
        async with self._open() as conn:
            await conn.execute("INSERT INTO notes (text) VALUES (?)", (text,))
            await asyncio.sleep(0)
            raise RuntimeError("failed before committing")

    async def texts(self):
        async with self._open() as conn:
            cursor = await conn.execute("SELECT text FROM notes ORDER BY id")
            return [row[0] for row in await cursor.fetchall()]

    async def add_while_holding(self, text):
        async with self._open():
            await self.add(text)

    async def first(self, as_row=False):
        async with self._open() as conn:
            if as_row:
                conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT text FROM notes ORDER BY id")
            return await cursor.fetchone()


def _create(path) -> str:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
    return str(path)


@pytest.fixture
def db_path(tmp_path):
    return _create(tmp_path / "library.db")


class TestWhenConnectionsAreShared:
    @pytest.mark.asyncio
    async def test_each_call_opens_its_own_outside_a_block(self, db_path, opened):
        notes = _Notes(db_path)
        await notes.add("a")
        await notes.texts()
        assert len(opened) == 2

    @pytest.mark.asyncio
    async def test_calls_inside_a_block_share_one(self, db_path, opened):
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            await notes.add("a")
            await notes.add("b")
            assert await notes.texts() == ["a", "b"]
        assert len(opened) == 1

    @pytest.mark.asyncio
    async def test_classes_that_spell_the_path_differently_share_it(
        self, db_path, opened
    ):
        """The enricher keeps its path as a resolved ``Path``, the indexer
        as the configured string."""
        async with shared_connection(db_path):
            await _Notes(db_path).add("a")
            await _Notes(Path(db_path).resolve()).add("b")
        assert len(opened) == 1

    @pytest.mark.asyncio
    async def test_a_path_through_a_symlink_shares_it(self, db_path, tmp_path, opened):
        """``Path.resolve()`` follows links, as on a system whose ``/home``
        is one."""
        link = tmp_path / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        async with shared_connection(link / "library.db"):
            await _Notes(str(link / "library.db")).add("a")
            await _Notes(Path(db_path).resolve()).add("b")
        assert len(opened) == 1

    @pytest.mark.asyncio
    async def test_another_database_is_not_shared(self, db_path, tmp_path, opened):
        other = _Notes(_create(tmp_path / "other.db"))
        async with shared_connection(db_path):
            await other.add("a")
            await other.add("b")
        assert len(opened) == 3

    @pytest.mark.asyncio
    async def test_a_block_inside_another_reuses_its_connection(
        self, db_path, opened
    ):
        async with shared_connection(db_path):
            async with shared_connection(db_path):
                await _Notes(db_path).add("a")
            await _Notes(db_path).add("b")
        assert len(opened) == 1

    @pytest.mark.asyncio
    async def test_the_connection_closes_with_the_block(self, db_path, opened):
        async with shared_connection(db_path):
            await _Notes(db_path).add("a")
        [connection] = opened
        with pytest.raises(ValueError):
            await connection.execute("SELECT 1")


class TestWhatACallCanStillRelyOn:
    @pytest.mark.asyncio
    async def test_work_left_uncommitted_is_discarded(self, db_path):
        """As closing a connection of its own would discard it, rather than
        letting the next call's commit take it along."""
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            await notes.add("lost", commit=False)
            await notes.add("kept")
        assert await notes.texts() == ["kept"]

    @pytest.mark.asyncio
    async def test_concurrent_calls_take_turns(self, db_path):
        """Interleaved on one connection, the second call's commit would
        take the first's insert with it before the first failed."""
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            outcomes = await asyncio.gather(
                notes.add_then_fail("lost"), notes.add("kept"),
                return_exceptions=True,
            )
        assert isinstance(outcomes[0], RuntimeError)
        assert await notes.texts() == ["kept"]

    @pytest.mark.asyncio
    async def test_rows_are_tuples_after_a_call_that_asked_for_more(
        self, db_path
    ):
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            await notes.add("a")
            assert dict(await notes.first(as_row=True)) == {"text": "a"}
            assert await notes.first() == ("a",)

    @pytest.mark.asyncio
    async def test_nothing_is_held_between_calls(self, db_path):
        """Other processes write the same database, and a scan lasts
        minutes: between calls the shared connection must neither stop
        them checkpointing nor read an old snapshot."""
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            await notes.add("a")
            await notes.add("b")
            await notes.first()

            with sqlite3.connect(db_path) as elsewhere:
                elsewhere.execute("INSERT INTO notes (text) VALUES ('c')")
            with sqlite3.connect(db_path) as elsewhere:
                busy, _, _ = elsewhere.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()

            assert busy == 0
            assert await notes.texts() == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_a_call_made_while_holding_the_connection_fails(self, db_path):
        """Rather than wait forever for the call that made it."""
        async with shared_connection(db_path):
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(_Notes(db_path).add_while_holding("a"), 5)
            await _Notes(db_path).add("b")
        assert await _Notes(db_path).texts() == ["b"]

    @pytest.mark.asyncio
    async def test_work_is_discarded_when_a_cursor_fails_to_close(
        self, db_path, monkeypatch
    ):
        notes = _Notes(db_path)
        async with shared_connection(db_path):
            async def failing_close(cursor):
                raise sqlite3.ProgrammingError("cannot close")

            with monkeypatch.context() as patched:
                patched.setattr(aiosqlite.Cursor, "close", failing_close)
                with pytest.raises(sqlite3.ProgrammingError):
                    await notes.add("lost", commit=False)
            await notes.add("kept")
        assert await notes.texts() == ["kept"]
