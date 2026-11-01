"""What a folder on a drive holds, as its files say, read on demand."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Optional, Sequence

from kalinka_plugin_sdk.files import is_hidden_file, is_housekeeping_dir
from kalinka_plugin_sdk.media.formats import is_supported_audio_file
from kalinka_plugin_sdk.media.tags import TagSummary, read_summary

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif")


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FolderContents:
    """One folder's own level: subfolders and playable files, by name."""

    folders: list[tuple[str, str]]
    tracks: list[FileEntry]
    has_images: bool


@dataclass(frozen=True)
class FolderCounts:
    folders: int
    tracks: int
    has_images: bool


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def folder_subname(counts: Optional[FolderCounts], held_in: Optional[str]) -> Optional[str]:
    """What a folder's row says under its name: what it holds, and the
    library holding it. None for one not counted that no library holds."""
    parts = []
    if counts is not None and counts.folders:
        parts.append(_counted(counts.folders, "folder"))
    if counts is not None and counts.tracks:
        parts.append(_counted(counts.tracks, "track"))
    if held_in:
        parts.append(f"In {held_in}")
    return " · ".join(parts) or ("Empty" if counts is not None else None)


def _by_name(name: str) -> tuple[str, str]:
    return (name.casefold(), name)


def shown(name: str) -> str:
    """``name`` as text to show: bytes in it that are not UTF-8, as an old
    ext4 stick or share may hold, become replacement marks."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _kind(entry: os.DirEntry) -> Optional[str]:
    """What a folder entry is to a listing: a ``folder``, a playable
    ``track``, an ``image``, or nothing to show. Links are not followed."""
    if entry.is_dir(follow_symlinks=False):
        return None if is_housekeeping_dir(entry.name) else "folder"
    if not entry.is_file(follow_symlinks=False) or is_hidden_file(entry.name):
        return None
    if is_supported_audio_file(entry.name):
        return "track"
    return "image" if entry.name.lower().endswith(_IMAGE_SUFFIXES) else None


def list_folder(path: str) -> FolderContents:
    """``path``'s subfolders and playable files, without following links.

    @raise OSError If the folder cannot be listed.
    """
    folders: list[tuple[str, str]] = []
    tracks: list[FileEntry] = []
    has_images = False
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                kind = _kind(entry)
                if kind == "folder":
                    folders.append((entry.name, entry.path))
                elif kind == "track":
                    st = entry.stat(follow_symlinks=False)
                    tracks.append(FileEntry(entry.name, entry.path, st.st_size, st.st_mtime_ns))
                elif kind == "image":
                    has_images = True
            except OSError:
                continue
    folders.sort(key=lambda f: _by_name(f[0]))
    tracks.sort(key=lambda t: _by_name(t.name))
    return FolderContents(folders, tracks, has_images)


def count_folder(path: str) -> FolderCounts:
    """How many subfolders and playable files ``path`` holds directly; none
    when it cannot be listed. Reads names only, no file's details."""
    counted = {"folder": 0, "track": 0, "image": 0}
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    kind = _kind(entry)
                except OSError:
                    continue
                if kind is not None:
                    counted[kind] += 1
    except OSError:
        return FolderCounts(0, 0, False)
    return FolderCounts(counted["folder"], counted["track"], counted["image"] > 0)


class ListingCache:
    """Folders listed lately, kept while their own modification time says
    nothing was added or taken out, so paging through a large folder lists
    it once.

    @note Thread-safe. A listing is kept for ``max_age_s`` at most, as a
        share may report a folder's time late.
    """

    def __init__(self, capacity: int = 8, max_age_s: float = 30.0):
        self._capacity = capacity
        self._max_age_s = max_age_s
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, float, FolderContents]] = OrderedDict()

    def list(self, path: str) -> FolderContents:
        """@raise OSError If the folder cannot be listed."""
        changed = os.stat(path).st_mtime_ns
        now = time.monotonic()
        with self._lock:
            kept = self._entries.get(path)
            if kept is not None and kept[0] == changed and now - kept[1] < self._max_age_s:
                self._entries.move_to_end(path)
                return kept[2]
        contents = list_folder(path)
        with self._lock:
            self._entries[path] = (changed, now, contents)
            self._entries.move_to_end(path)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return contents


def walk_tracks(path: str, limit: int) -> list[str]:
    """Every playable file below ``path``, at most ``limit``, in the order the
    listings show them: each folder's subfolders before its own tracks."""
    found: list[str] = []

    def visit(folder: str) -> None:
        try:
            contents = list_folder(folder)
        except OSError:
            return
        for _, sub in contents.folders:
            if len(found) >= limit:
                return
            visit(sub)
        found.extend(t.path for t in contents.tracks[: max(0, limit - len(found))])

    visit(path)
    return found


class TagCache:
    """Tags of files read lately, so a page shown again reads nothing.

    @note Thread-safe; reads run on a small pool of its own, so a slow drive
        holds up only the tracks it serves.
    """

    def __init__(self, capacity: int = 5000, workers: int = 4):
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, int, int], TagSummary] = OrderedDict()
        self._reading: dict[tuple[str, int, int], Future] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kal-tags")

    def cached(self, key: tuple[str, int, int]) -> tuple[bool, Optional[TagSummary]]:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return True, self._entries[key]
        return False, None

    def _read(self, key: tuple[str, int, int]) -> Optional[TagSummary]:
        try:
            summary = read_summary(key[0])
        except Exception:  # noqa: BLE001 — unplugged mid-read, or a file no reader takes
            summary = None
        with self._lock:
            # Nothing is kept for a file that did not read: pulled out mid-read,
            # it reads again once the drive is back.
            if summary is not None:
                self._entries[key] = summary
                self._entries.move_to_end(key)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            self._reading.pop(key, None)
        return summary

    def _reader(self, key: tuple[str, int, int]) -> Future:
        """The read of ``key`` under way, or a new one: a page shown twice
        while its tags are read reads each file once."""
        with self._lock:
            future = self._reading.get(key)
            if future is None:
                future = self._reading[key] = self._pool.submit(self._read, key)
        return future

    def read_all(
        self, files: Sequence[FileEntry], budget_s: float
    ) -> dict[str, Optional[TagSummary]]:
        """Tags for ``files`` by path; a file not read within ``budget_s``
        is left out, and read in the background for next time."""
        out: dict[str, Optional[TagSummary]] = {}
        pending = {}
        for entry in files:
            key = (entry.path, entry.size, entry.mtime_ns)
            hit, summary = self.cached(key)
            if hit:
                out[entry.path] = summary
            else:
                pending[self._reader(key)] = entry.path
        if pending:
            done, _ = wait(pending, timeout=max(0.0, budget_s))
            for future in done:
                out[pending[future]] = future.result()
        return out

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def entry_of(path: str) -> Optional[FileEntry]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return FileEntry(os.path.basename(path), path, st.st_size, st.st_mtime_ns)

