"""My Library without sqlite-vec, as on 32-bit ARM, where it has no wheel and
is installed only with AI search: the library itself still works."""

import sqlite3
import sys
from pathlib import Path

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


def _config(tmp_path) -> LocalFilesConfig:
    return LocalFilesConfig(
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )


def _tables(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


@pytest.mark.asyncio
async def test_the_library_schema_is_set_up_without_vector_tables(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    config = _config(tmp_path)

    await init_db(config.db_path)

    tables = _tables(config.db_path)
    assert {"tracks", "albums", "artists", "library_file"} <= tables
    assert not any(name.startswith("vec_") for name in tables)


@pytest.mark.asyncio
async def test_a_library_moved_from_a_box_with_vectors_still_rebuilds(
    tmp_path, monkeypatch
):
    """Its vector tables cannot be dropped without the extension, so the
    rebuild removes the database file instead."""
    pytest.importorskip("sqlite_vec")
    config = _config(tmp_path)
    await init_db(config.db_path)
    assert any(name.startswith("vec_") for name in _tables(config.db_path))
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)

    LocalFilesInputModuleDb(config).purge_all()

    assert not Path(config.db_path).exists()
    await init_db(config.db_path)
    assert "tracks" in _tables(config.db_path)
