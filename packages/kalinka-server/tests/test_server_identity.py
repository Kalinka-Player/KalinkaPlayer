"""Core's id, read and made by server_id_file, which the package's helper is.

The package makes the id as it installs. Core makes one only where that has
not run, and has to start on whatever it finds in the id's place.
"""

import logging
import os
import uuid

import pytest

from kalinka_server import server_id_file, server_identity

KEPT_ID = "0b0c5a3e-8c1b-4a51-9a0d-6c9b1c7f1e2a"


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Core's state directory under tmp_path, with no id read yet."""
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.setattr(server_identity, "_server_id", None)
    return tmp_path / "var" / "lib" / "kalinka"


def test_core_takes_the_id_the_package_made(state):
    state.mkdir(parents=True)
    (state / "server_id").write_text(KEPT_ID.upper() + "\n")

    assert server_identity.get_server_id() == KEPT_ID
    assert (state / "server_id").read_text() == KEPT_ID.upper() + "\n"


def test_core_makes_an_id_where_the_package_made_none(state, monkeypatch):
    made = server_identity.get_server_id()

    assert uuid.UUID(made).version == 4
    assert (state / "server_id").read_text() == made + "\n"
    monkeypatch.setattr(server_identity, "_server_id", None)
    assert server_identity.get_server_id() == made


@pytest.mark.parametrize(
    "left", [b"", b"not-a-uuid\n", b"\xff\xfe\n"], ids=["empty", "malformed", "binary"]
)
def test_core_replaces_whatever_else_is_in_the_ids_place(left, state):
    """Bytes that are not text stopped Core's start before it read the id the
    way the package does."""
    state.mkdir(parents=True)
    (state / "server_id").write_bytes(left)

    made = server_identity.get_server_id()

    assert uuid.UUID(made).version == 4
    assert (state / "server_id").read_text() == made + "\n"


def test_core_runs_on_a_temporary_id_where_it_cannot_write_one(
    monkeypatch, tmp_path, caplog
):
    not_a_directory = tmp_path / "prefix"
    not_a_directory.touch()
    monkeypatch.setenv("KALINKA_PREFIX", str(not_a_directory))
    monkeypatch.setattr(server_identity, "_server_id", None)

    with caplog.at_level(logging.WARNING):
        made = server_identity.get_server_id()

    assert uuid.UUID(made).version == 4
    assert server_identity.get_server_id() == made
    assert "using an ephemeral id" in caplog.text


def _directory_owned_by(monkeypatch, directory, uid, gid):
    real_stat = os.stat

    def stat(target, *args, **kwargs):
        st = real_stat(target, *args, **kwargs)
        if target != directory:
            return st
        return os.stat_result(
            (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid, gid, st.st_size)
            + (0, 0, 0)
        )

    monkeypatch.setattr(os, "stat", stat)


def _fchown_calls(monkeypatch, euid):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    return calls


def test_root_gives_a_new_id_to_the_state_directorys_owner(monkeypatch, tmp_path):
    """The postinst makes it as root, for Core, which runs as kalusr."""
    _directory_owned_by(monkeypatch, tmp_path, 4242, 4343)
    calls = _fchown_calls(monkeypatch, euid=0)

    server_id_file.ensure_id(tmp_path / "server_id")

    assert calls == [(4242, 4343)]


def test_root_leaves_an_owner_that_is_already_the_directorys(monkeypatch, tmp_path):
    calls = _fchown_calls(monkeypatch, euid=0)

    server_id_file.ensure_id(tmp_path / "server_id")

    assert calls == []


def test_core_gives_nothing_away_in_a_directory_another_user_owns(
    monkeypatch, tmp_path
):
    """Only root may, so trying would cost Core its id in a dev run."""
    _directory_owned_by(monkeypatch, tmp_path, 4242, 4343)
    calls = _fchown_calls(monkeypatch, euid=1000)

    made = server_id_file.ensure_id(tmp_path / "server_id")

    assert calls == []
    assert (tmp_path / "server_id").read_text() == made + "\n"
