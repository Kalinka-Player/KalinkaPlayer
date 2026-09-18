#!/usr/bin/env python3
"""Indexing a library the indexer cannot reach through the filesystem.

The storage here is an in-memory one behind a scheme of its own, so nothing
in the module has ever heard of it. That is the point: what these tests prove
is that the indexer no longer knows where files live. Everything it does with
one — walk to it, measure it, read its tags, find the cue sheet and the
sleeve beside it, recognise it after a rename, serve it — has to work through
storage that is not the local filesystem and is not SMB either.

Change notification is deliberately absent, because most protocols cannot do
it and the library has to be correct without it.
"""

import io
import itertools
import time
from dataclasses import dataclass
from typing import BinaryIO, Optional

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.storage import (
    DirEntry,
    FileIdentity,
    FileStat,
    FileStorage,
    RootStatus,
    StorageResolver,
    is_within,
    scheme_of,
)

SCHEME = "vault"
ROOT = f"{SCHEME}://box/music"


@dataclass
class _Node:
    data: bytes
    mtime_ns: int
    inode: str


class VaultStorage(FileStorage):
    """An in-memory library reachable only through this object."""

    def __init__(self, spill_dir=None):
        super().__init__(spill_dir=spill_dir)
        self.nodes: dict[str, _Node] = {}
        self.available = True
        self.identify = True
        self._inodes = itertools.count(1)
        self.trips: list[tuple[str, str]] = []


    def add(self, path: str, data: bytes) -> str:
        self.nodes[path] = _Node(
            data=data, mtime_ns=time.time_ns(), inode=str(next(self._inodes))
        )
        return path

    def remove(self, path: str) -> None:
        del self.nodes[path]

    def rename(self, old: str, new: str) -> None:
        """A move that keeps the file's identity, as a real one does."""
        self.nodes[new] = self.nodes.pop(old)


    @property
    def scheme(self) -> str:
        return SCHEME

    def handles(self, path: str) -> bool:
        return scheme_of(path) == SCHEME

    def canonical(self, path: str) -> str:
        return path.rstrip("/")

    def contains(self, path: str, roots) -> bool:
        return any(is_within(path, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        self.trips.append(("listdir", path))
        self._require_online()
        prefix = path.rstrip("/") + "/"
        children: dict[str, bool] = {}
        for key in self.nodes:
            if not key.startswith(prefix):
                continue
            name, separator, _ = key[len(prefix):].partition("/")
            children[name] = bool(separator)
        if not children and path not in self._directories():
            raise OSError(f"no such directory: {path}")
        return [
            DirEntry(name=name, path=prefix + name, is_dir=is_dir)
            for name, is_dir in sorted(children.items())
        ]

    def stat(self, path: str) -> FileStat:
        self.trips.append(("stat", path))
        self._require_online()
        node = self.nodes.get(path)
        if node is None:
            if path in self._directories():
                return FileStat(size=0, mtime_ns=0, is_dir=True)
            raise OSError(f"no such path: {path}")
        identity = (
            FileIdentity(device="vault", inode=node.inode)
            if self.identify
            else None
        )
        return FileStat(
            size=len(node.data),
            mtime_ns=node.mtime_ns,
            is_dir=False,
            identity=identity,
        )

    def open(self, path: str) -> BinaryIO:
        self.trips.append(("open", path))
        self._require_online()
        node = self.nodes.get(path)
        if node is None:
            raise OSError(f"no such file: {path}")
        return io.BytesIO(node.data)

    def probe_root_blocking(self, root: str) -> RootStatus:
        self.trips.append(("probe", root))
        if not self.available:
            return self.unavailable(root, "the vault is offline")
        return RootStatus(
            root=root,
            available=True,
            reason="",
            fs_type=SCHEME,
            is_network=True,
            is_autofs=False,
            identity="vault box",
        )

    def local_path(self, path: str) -> Optional[str]:
        return None


    def _require_online(self) -> None:
        if not self.available:
            raise OSError("the vault is offline")

    def _directories(self) -> set[str]:
        dirs = {ROOT}
        for key in self.nodes:
            parts = key[len(ROOT) + 1:].split("/")[:-1]
            for depth in range(len(parts)):
                dirs.add("/".join([ROOT, *parts[: depth + 1]]))
        return dirs


def _flac(tmp_path, tags, cover=None) -> bytes:
    scratch = tmp_path / "scratch.flac"
    sf.write(str(scratch), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(scratch))
    for key, value in tags.items():
        audio[key] = value
    if cover is not None:
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.data = cover
        audio.add_picture(picture)
    audio.save()
    data = scratch.read_bytes()
    scratch.unlink()
    return data


def _png(colour=(120, 30, 200), size=(1000, 1000)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest_asyncio.fixture
async def library(tmp_path):
    vault = VaultStorage(spill_dir=str(tmp_path / "spill"))
    config = LocalFilesConfig(
        music_folders=[ROOT],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    indexer = FileIndexer(
        config, AsyncIndexerDb(config), storage=StorageResolver([vault])
    )
    return indexer, vault, config


class TestScanning:
    @pytest.mark.asyncio
    async def test_the_library_is_indexed_from_its_tags(self, library, tmp_path):
        indexer, vault, _ = library
        vault.add(
            f"{ROOT}/Roxy Music/Avalon/01 More Than This.flac",
            _flac(tmp_path, {"title": "More Than This", "artist": "Roxy Music",
                             "album": "Avalon", "tracknumber": "1"}),
        )
        vault.add(
            f"{ROOT}/Roxy Music/Avalon/02 Avalon.flac",
            _flac(tmp_path, {"title": "Avalon", "artist": "Roxy Music",
                             "album": "Avalon", "tracknumber": "2"}),
        )

        await indexer.run_scan()

        tracks = await indexer.db_manager.get_all_tracks()
        assert sorted(t["title"] for t in tracks) == ["Avalon", "More Than This"]
        assert {t["file_path"] for t in tracks} == set(vault.nodes)

    @pytest.mark.asyncio
    async def test_the_walk_reaches_every_depth(self, library, tmp_path):
        indexer, vault, _ = library
        data = _flac(tmp_path, {"title": "Deep", "artist": "A", "album": "B"})
        vault.add(f"{ROOT}/a/b/c/d/deep.flac", data)
        vault.add(f"{ROOT}/shallow.flac", data)

        listed = await indexer._audio_files_by_folder([ROOT])
        await indexer.run_scan()

        assert len(listed[ROOT]) == 2
        assert len(await indexer.db_manager.get_all_tracks()) == 2

    @pytest.mark.asyncio
    async def test_the_library_is_walked_once_per_scan(self, library, tmp_path):
        """Every directory in the walk is a round trip, so walking once to
        count the work and again to do it would pay for the whole library
        twice — and the count is what the progress bar is made of."""
        indexer, vault, _ = library
        data = _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"})
        for folder in ("Avalon", "Manifesto", "Siren"):
            vault.add(f"{ROOT}/{folder}/01 track.flac", data)

        walks = []
        walk = indexer._iter_audio_files
        indexer._iter_audio_files = lambda folder: walks.append(folder) or walk(folder)

        await indexer.run_scan()

        assert walks == [ROOT]
        progress = await indexer.db_manager.get_scan_progress()
        assert (progress["total"], progress["processed"]) == (3, 3)

    @pytest.mark.asyncio
    async def test_files_of_other_kinds_are_left_alone(self, library, tmp_path):
        indexer, vault, _ = library
        vault.add(f"{ROOT}/a.flac", _flac(tmp_path, {"title": "A"}))
        vault.add(f"{ROOT}/notes.txt", b"not music")
        vault.add(f"{ROOT}/cover.jpg", _png())

        await indexer.run_scan()

        assert len(await indexer.db_manager.get_all_tracks()) == 1

    @pytest.mark.asyncio
    async def test_an_offline_vault_is_not_a_purge(self, library, tmp_path):
        """The one behaviour a share must never lose: going away is not the
        same as being emptied."""
        indexer, vault, _ = library
        vault.add(f"{ROOT}/a.flac", _flac(tmp_path, {"title": "A"}))
        await indexer.run_scan()
        assert len(await indexer.db_manager.get_all_tracks()) == 1

        vault.available = False
        await indexer.run_scan()

        assert len(await indexer.db_manager.get_all_tracks()) == 1

    @pytest.mark.asyncio
    async def test_a_deleted_file_is_purged_while_the_vault_answers(
        self, library, tmp_path
    ):
        indexer, vault, _ = library
        vault.add(f"{ROOT}/a.flac", _flac(tmp_path, {"title": "A"}))
        vault.add(f"{ROOT}/b.flac", _flac(tmp_path, {"title": "B"}))
        await indexer.run_scan()

        vault.remove(f"{ROOT}/a.flac")
        await indexer.run_scan()

        titles = [t["title"] for t in await indexer.db_manager.get_all_tracks()]
        assert titles == ["B"]


class TestWhatIsReadBesideTheAudio:
    @pytest.mark.asyncio
    async def test_a_cue_sheet_fills_a_tagless_rip(self, library, tmp_path):
        indexer, vault, _ = library
        vault.add(f"{ROOT}/rip/album.flac", _flac(tmp_path, {}))
        vault.add(
            f"{ROOT}/rip/album.cue",
            b'PERFORMER "Roxy Music"\nTITLE "Avalon"\n'
            b'FILE "album.flac" WAVE\n  TRACK 01 AUDIO\n'
            b'    TITLE "More Than This"\n    INDEX 01 00:00:00\n',
        )

        await indexer.run_scan()

        [track] = await indexer.db_manager.get_all_tracks()
        assert track["title"] == "More Than This"

    @pytest.mark.asyncio
    async def test_a_sleeve_in_the_folder_becomes_the_cover(
        self, library, tmp_path
    ):
        indexer, vault, config = library
        vault.add(
            f"{ROOT}/Avalon/01 track.flac",
            _flac(tmp_path, {"title": "T", "artist": "Roxy Music",
                             "album": "Avalon"}),
        )
        vault.add(f"{ROOT}/Avalon/cover.png", _png())

        await indexer.run_scan()

        [track] = await indexer.db_manager.get_all_tracks()
        album = await indexer.db_manager.get_album_by_id(track["album_id"])
        assert album["image_url"] == f"{album['id']}.jpg"
        assert (
            indexer.artwork_path / "album" / f"{album['id']}_large.jpg"
        ).exists()

    @pytest.mark.asyncio
    async def test_an_embedded_cover_is_read_from_the_stream(
        self, library, tmp_path
    ):
        indexer, vault, _ = library
        vault.add(
            f"{ROOT}/single.flac",
            _flac(tmp_path, {"title": "Bee Moved"}, cover=_png((250, 200, 20))),
        )

        changes = await indexer.process_file(f"{ROOT}/single.flac")

        track_id = changes["tracks"]
        track = await indexer.db_manager.get_track_by_id(track_id)
        assert track["image_url"] == f"{track_id}.jpg"


class TestIdentity:
    @pytest.mark.asyncio
    async def test_a_renamed_file_keeps_its_place_in_the_library(
        self, library, tmp_path
    ):
        indexer, vault, _ = library
        old = vault.add(
            f"{ROOT}/01 old name.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        changes = await indexer.process_file(old)
        track_id = changes["tracks"]

        new = f"{ROOT}/01 new name.flac"
        vault.rename(old, new)
        await indexer.process_file(new)

        track = await indexer.db_manager.get_track_by_id(track_id)
        assert track is not None
        assert track["file_path"] == new

    @pytest.mark.asyncio
    async def test_storage_that_will_not_identify_a_file_still_indexes(
        self, library, tmp_path
    ):
        """Without an identity a rename reads as a new file, which costs
        continuity and nothing else."""
        indexer, vault, _ = library
        vault.identify = False
        old = vault.add(
            f"{ROOT}/01 old.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        first = await indexer.process_file(old)

        new = f"{ROOT}/01 new.flac"
        vault.rename(old, new)
        second = await indexer.process_file(new)

        assert first["tracks"] != second["tracks"]
        assert (await indexer.db_manager.get_track_by_id(second["tracks"]))


class TestChangeNotification:
    def test_the_vault_cannot_report_changes(self, library):
        _, vault, _ = library
        assert vault.watcher() is None

    @pytest.mark.asyncio
    async def test_the_watcher_worker_gives_up_rather_than_idling(
        self, library, monkeypatch
    ):
        """With nothing watchable configured there is no loop worth running,
        and the periodic scan is what picks new files up."""
        import kalinka_plugin_localfiles.indexer.indexer as indexer_mod

        _, vault, config = library
        monkeypatch.setattr(
            indexer_mod, "build_resolver", lambda _config: StorageResolver([vault])
        )

        await indexer_mod._file_watcher_worker(config)


def _module_over(config, vault) -> LocalFilesInputModule:
    """The input module reading through the vault and nothing else."""
    resolver = StorageResolver([vault])
    module = LocalFilesInputModule(
        config, LocalFilesInputModuleDb(config), storage_source=lambda: resolver
    )
    module._music_folders = [ROOT]
    return module


class TestServingATrack:
    @pytest.mark.asyncio
    async def test_the_server_is_handed_a_stream_and_a_length(
        self, library, tmp_path
    ):
        """There is no file to name, so the module offers the bytes instead —
        with the length, because a renderer seeks by byte range."""
        indexer, vault, config = library
        data = _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"})
        path = vault.add(f"{ROOT}/01 track.flac", data)
        changes = await indexer.process_file(path)

        module = _module_over(config, vault)

        info = await module.get_content_info(changes["tracks"])

        assert info.local_path is None
        assert info.size == len(data)
        assert info.mime_type.startswith("audio/")
        with info.reader() as stream:
            assert stream.read() == data

    @pytest.mark.asyncio
    async def test_serving_costs_one_measurement_and_the_open(
        self, library, tmp_path
    ):
        """A renderer seeks by asking for byte ranges, so this runs per
        request. Establishing that the file is there, how long it is and
        whether it can be read is one round trip, not three: the open the
        server makes next is what answers the last of them."""
        indexer, vault, config = library
        path = vault.add(
            f"{ROOT}/01 track.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        changes = await indexer.process_file(path)
        module = _module_over(config, vault)

        vault.trips.clear()
        info = await module.get_content_info(changes["tracks"])

        assert [kind for kind, _ in vault.trips] == ["probe", "stat"]
        assert info.size is not None
        with info.reader():
            pass

    @pytest.mark.asyncio
    async def test_a_track_that_went_away_is_absent(self, library, tmp_path):
        """The measurement is the existence check too, so a deleted file has
        to still read as absent rather than as a broken stream."""
        indexer, vault, config = library
        path = vault.add(
            f"{ROOT}/01 track.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        changes = await indexer.process_file(path)
        module = _module_over(config, vault)
        vault.remove(path)

        assert await module.get_content_info(changes["tracks"]) is None

    @pytest.mark.asyncio
    async def test_a_track_outside_the_folders_is_absent(self, library, tmp_path):
        """The boundary check is the one thing the measurement does not
        replace: a folder can leave the configuration between queueing a
        track and serving it."""
        indexer, vault, config = library
        path = vault.add(
            f"{ROOT}/01 track.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        changes = await indexer.process_file(path)
        module = _module_over(config, vault)
        module._music_folders = [f"{SCHEME}://box/elsewhere"]

        assert await module.get_content_info(changes["tracks"]) is None

    @pytest.mark.asyncio
    async def test_an_offline_vault_refuses_to_serve(self, library, tmp_path):
        indexer, vault, config = library
        path = vault.add(
            f"{ROOT}/01 track.flac",
            _flac(tmp_path, {"title": "T", "artist": "A", "album": "B"}),
        )
        changes = await indexer.process_file(path)

        module = _module_over(config, vault)
        vault.available = False

        from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

        [info] = await module.get_track_info([changes["tracks"]])
        with pytest.raises(SourceUnavailableError):
            await info.source_retriever()
