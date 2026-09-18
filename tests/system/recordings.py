"""The music the test plants: public-domain piano recordings from the Internet Archive.

Two albums by one pianist, so each storage holds a whole album of its own
while a search for the artist has to reach both. The MP3 derivatives carry
full ID3 tags (artist, album, title, track number, cover), which is what
makes them indexable and, via MusicBrainz, enrichable.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from mutagen import File as audio_file

logger = logging.getLogger("system-test")

_ARCHIVE = "https://archive.org/download"


@dataclass(frozen=True)
class Recording:
    """One track as the Internet Archive stores it and as the test files it."""

    item: str
    archive_name: str
    planted_name: str

    @property
    def url(self) -> str:
        return f"{_ARCHIVE}/{self.item}/{quote(self.archive_name)}"


@dataclass(frozen=True)
class Album:
    folder: str
    recordings: tuple[Recording, ...]


def _album(item: str, stem: str, folder: str, titles: dict[int, str]) -> Album:
    return Album(
        folder=folder,
        recordings=tuple(
            Recording(
                item=item,
                archive_name=f"{stem} - {number:02d} {title}.mp3",
                planted_name=f"{number:02d} {title.rstrip('.')}.mp3",
            )
            for number, title in titles.items()
        ),
    )


GOLDBERG = _album(
    "OpenGoldbergVariations",
    'Kimiko Ishizaka - J.S. Bach- -Open- Goldberg Variations, BWV 988 (Piano)',
    "Open Goldberg Variations",
    {
        1: "Aria",
        2: "Variatio 1 a 1 Clav.",
        3: "Variatio 2 a 1 Clav.",
        4: "Variatio 3 a 1 Clav. Canone all Unisuono",
        5: "Variatio 4 a 1 Clav.",
    },
)

ART_OF_FUGUE = _album(
    "pandacd-715-js-bach-the-art-of-the-fugue-kunst-der-fuge-bwv-1080",
    "Kimiko Ishizaka - J.S. Bach- The Art of the Fugue (Kunst der Fuge), BWV 1080",
    "The Art of the Fugue",
    {
        1: "Contrapunctus 1",
        2: "Contrapunctus 2",
        3: "Contrapunctus 3",
        4: "Contrapunctus 4",
        5: "Contrapunctus 5",
    },
)

ARTIST = "Kimiko Ishizaka"


def _is_complete_audio(path: Path) -> bool:
    try:
        parsed = audio_file(path)
    except Exception:
        return False
    return parsed is not None and parsed.info.length > 0


def fetch(recording: Recording, cache: Path) -> Path:
    """The recording's file in ``cache``, downloaded on first use."""
    target = cache / "tracks" / recording.item / recording.planted_name
    if target.exists() and _is_complete_audio(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    logger.info("downloading %s", recording.url)
    with httpx.stream(
        "GET",
        recording.url,
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=30.0),
    ) as response:
        response.raise_for_status()
        with open(part, "wb") as out:
            for chunk in response.iter_bytes(1 << 16):
                out.write(chunk)
    if not _is_complete_audio(part):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{recording.url} did not download as a playable file")
    part.replace(target)
    return target


@dataclass(frozen=True)
class PlantedTrack:
    """A recording placed where the library will find it.

    ``library_path`` is the location as the indexer records it: the resolved
    local path, or the ``smb://`` URL for a file on the share.
    """

    recording: Recording
    source: Path
    path: Path
    library_path: str

    @property
    def content(self) -> bytes:
        return self.source.read_bytes()

    def remove(self) -> None:
        self.path.unlink()

    def restore(self) -> None:
        shutil.copyfile(self.source, self.path)


def plant(
    album: Album, cache: Path, root: Path, library_root: str
) -> list[PlantedTrack]:
    """Copy ``album`` into ``root/<album folder>``. ``library_root`` is how
    the library spells ``root``: the path itself, or the share URL."""
    folder = root / album.folder
    folder.mkdir(parents=True, exist_ok=True)
    planted = []
    for recording in album.recordings:
        source = fetch(recording, cache)
        path = folder / recording.planted_name
        shutil.copyfile(source, path)
        planted.append(
            PlantedTrack(
                recording=recording,
                source=source,
                path=path,
                library_path=f"{library_root}/{album.folder}/{recording.planted_name}",
            )
        )
    return planted
