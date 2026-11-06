"""Opt-in Samba tests without external recordings, models or a Kalinka server.

KALINKA_SMB_TEST=1 .venv/bin/python -m pytest \
    packages/kalinka-plugin-localfiles/tests/test_smb_notifications_live.py
"""

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import smbclient
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer import indexer as module
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.storage.smb import SmbCredentials, SmbStorage

pytestmark = pytest.mark.skipif(
    os.environ.get("KALINKA_SMB_TEST") != "1",
    reason="set KALINKA_SMB_TEST=1 for Samba tests",
)


def podman(*args, check=True):
    result = subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=180
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


@pytest.fixture
def share(tmp_path):
    if shutil.which("podman") is None:
        pytest.fail("SMB integration tests need rootless podman")
    image = "localhost/kalinka-system-test-smb:latest"
    context = Path(__file__).resolve().parents[3] / "tests/system/smb"
    if subprocess.run(["podman", "image", "exists", image]).returncode:
        podman("build", "-q", "-t", image, str(context))
    music = tmp_path / "music"
    music.mkdir()
    conf = tmp_path / "smb.conf"
    # Only the account share is writable; public remains guest/read-only.
    conf.write_text(
        (context / "smb.conf")
        .read_text()
        .replace("read only = yes", "read only = no", 1)
    )
    name = "kalinka-notify-test-" + uuid.uuid4().hex[:10]
    storage = SmbStorage(SmbCredentials(username="kalinka", password="kalinka-pass"))
    cache = {}
    try:
        podman(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::445",
            "-v",
            f"{music}:/music:Z",
            "-v",
            f"{conf}:/etc/samba/smb.conf:ro,Z",
            image,
        )
        port = int(podman("port", name, "445/tcp").rsplit(":", 1)[-1])
        root = f"smb://127.0.0.1:{port}/music"
        deadline = time.monotonic() + 20
        while not storage.probe_root_blocking(root).available:
            if time.monotonic() > deadline:
                pytest.fail(podman("logs", name))
            time.sleep(0.1)
        yield SimpleNamespace(
            name=name,
            music=music,
            root=root,
            port=port,
            login=dict(
                username="kalinka",
                password="kalinka-pass",
                port=port,
                connection_cache=cache,
            ),
        )
    finally:
        storage.close()
        smbclient.reset_connection_cache(connection_cache=cache, fail_on_error=False)
        podman("rm", "-f", name, check=False)


def audio(tmp_path, title):
    path = tmp_path / "track.flac"
    sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    tags = FLAC(path)
    tags["title"], tags["artist"], tags["album"] = title, "Artist", "Album"
    tags.save()
    return path.read_bytes()


def unc(relative):
    return "\\\\127.0.0.1\\music\\" + relative.replace("/", "\\")


def write(share, relative, data):
    with smbclient.open_file(
        unc(relative), "wb", share_access="rwd", **share.login
    ) as handle:
        handle.write(data)


async def until(check, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for {description}")


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_notifications_update_the_real_indexer_and_recover_after_disconnect(
    share, tmp_path, monkeypatch, overflow, caplog
):
    from kalinka_plugin_localfiles.indexer.source_retries import SourceRetries

    monkeypatch.setattr(
        module, "SourceRetries",
        lambda: SourceRetries(initial_delay=0.2, max_delay=1.0),
    )
    if overflow:
        from kalinka_plugin_localfiles.storage.smb_watcher import _Notification

        start = _Notification.start

        def small_buffer(self, completion_filter, flags):
            return start(self, completion_filter, flags, output_buffer_length=16)

        monkeypatch.setattr(_Notification, "start", small_buffer)
    config = LocalFilesConfig(
        music_sources=[
            dict(
                id="smb",
                kind="smb",
                location=dict(host="127.0.0.1", port=share.port, path="music"),
                authentication=dict(
                    mode="account", username="kalinka", password="kalinka-pass"
                ),
            )
        ],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "art"),
        scan_interval_minutes=0,
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    db = AsyncIndexerDb(config)
    monkeypatch.setattr(module, "_indexer_queue", asyncio.Queue())
    monkeypatch.setattr(module, "_shutdown_event", asyncio.Event())
    monkeypatch.setattr(module, "_file_watcher_stop_event", asyncio.Event())
    monkeypatch.setattr(module, "WATCH_POLL_INTERVAL_S", 0.1)
    monkeypatch.setattr(module, "REARM_CHECK_INTERVAL_S", 0.2)
    watches = []
    original = SmbStorage.watcher

    def make_watcher(storage):
        watcher = original(storage)
        watches.append(watcher)
        return watcher

    monkeypatch.setattr(SmbStorage, "watcher", make_watcher)
    indexer = asyncio.create_task(module._indexer_worker(config, db))
    watcher = asyncio.create_task(module._file_watcher_worker(config))

    async def armed():
        return watches and share.root in watches[0]._watches

    async def row(path, title=None):
        track = await db.get_track_by_path(share.root + "/" + path)
        return track if track and (title is None or track["title"] == title) else None

    try:
        await until(armed, "SMB subscription")
        await asyncio.to_thread(smbclient.makedirs, unc("Artist/Album"), **share.login)
        await asyncio.to_thread(
            write, share, "Artist/Album/track.flac", audio(tmp_path, "First")
        )
        first = await until(
            lambda: row("Artist/Album/track.flac", "First"), "new track"
        )
        await module._indexer_queue.join()
        if overflow:
            # Without event details reconciliation compares size/mtime;
            # make this edit visible at the database's timestamp precision.
            await asyncio.sleep(1.05)
        await asyncio.to_thread(
            write, share, "Artist/Album/track.flac", audio(tmp_path, "Other")
        )
        await until(lambda: row("Artist/Album/track.flac", "Other"), "retagged track")
        await module._indexer_queue.join()

        await asyncio.to_thread(
            smbclient.rename, unc("Artist/Album"), unc("Artist/Renamed"), **share.login
        )
        moved = await until(lambda: row("Artist/Renamed/track.flac"), "renamed album")
        assert moved["id"] == first["id"]
        await module._indexer_queue.join()

        await asyncio.to_thread(podman, "stop", "-t", "1", share.name)

        async def lost():
            return share.root not in watches[0]._watches

        await until(lost, "lost subscription")
        assert await row("Artist/Renamed/track.flac")  # outage is not deletion
        # A write queued before disconnect may reach the indexer only after
        # the NAS has gone. It must suspend the source, not spin on the file.
        await module._indexer_queue.put({"incremental_changes": {
            ("file_closed", share.root + "/Artist/Renamed/track.flac")
        }})
        await asyncio.wait_for(module._indexer_queue.join(), timeout=10)
        assert any("pausing file retries" in record.message for record in caplog.records)
        (share.music / "Artist/Renamed/track.flac").unlink()
        (share.music / "offline.flac").write_bytes(audio(tmp_path, "While offline"))
        await asyncio.to_thread(podman, "start", share.name)
        await until(
            lambda: row("offline.flac", "While offline"), "reconnect reconciliation"
        )

        async def old_gone():
            return await row("Artist/Renamed/track.flac") is None

        await until(old_gone, "offline deletion reconciliation")
        await asyncio.to_thread(smbclient.remove, unc("offline.flac"), **share.login)

        async def empty():
            return not await db.get_all_tracks()

        await until(empty, "last track deletion")
    finally:
        module._shutdown_event.set()
        module._file_watcher_stop_event.set()
        await asyncio.wait_for(asyncio.gather(indexer, watcher), timeout=10)
    assert not watches[0]._watches


@pytest.mark.parametrize("guest,encrypt", [(True, False), (False, True)])
def test_guest_and_encrypted_subscriptions(share, guest, encrypt):
    credentials = (
        SmbCredentials()
        if guest
        else SmbCredentials(
            username="kalinka", password="kalinka-pass", encrypt=encrypt
        )
    )
    storage = SmbStorage(credentials)
    watcher = storage.watcher()
    root = share.root.replace("/music", "/public") if guest else share.root
    try:
        assert watcher.watch(root)
        watcher.poll(0.1)
        (share.music / "new.flac").write_bytes(b"audio")
        deadline = time.monotonic() + 5
        changes = set()
        while time.monotonic() < deadline and not changes:
            changes.update(watcher.poll(0.1).changes)
        assert ("file_closed", root + "/new.flac") in changes
    finally:
        watcher.close()
        storage.close()
