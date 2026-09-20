"""The Song Describer Dataset, fetched and made safe to index.

Safe means two things. The library the server sees carries no text a caption
could have leaked into: every tag is deleted and every file is renamed to a
digest. And the pairing a caption came with — caption_id -> track_id — is kept
here, out of the library, as the only ground truth the scorer reads.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import statistics
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import Layout

ZENODO_RECORD = "10072001"
_BASE = f"https://zenodo.org/records/{ZENODO_RECORD}/files"

#: Published md5s from the Zenodo record, pinned so a silent re-upload or a
#: truncated download is a failure here rather than a number in the report.
FILES: dict[str, str] = {
    "song_describer.csv": "e90e9459c22bfbe69f5462dc1434d573",
    "audio.zip": "2126b8facfe9468cf806c6154e09bbe5",
    # The MTG-Jamendo row behind each recording: genre, instrument and
    # mood/theme tags, which the mood suite scores against.
    "song_describer_14_04_23.mtg-jamendo.tsv": "3532f2df8b4c21a7ea85d9121eae244a",
}

def opaque_name(track_id: str) -> str:
    """The name the library sees: the track id digested, so the filename
    carries no caption text, no Jamendo id and no directory structure."""
    return hashlib.sha1(f"sdd:{track_id}".encode()).hexdigest() + ".mp3"


def kalinka_track_id(file_path: Path) -> str:
    """The id the server will mint for this file — see utils.id_generator."""
    digest = hashlib.md5(str(file_path).encode("utf-8")).hexdigest()[:16]
    return f"track_{digest}"


@dataclass(frozen=True)
class Caption:
    caption_id: str
    track_id: str
    text: str
    is_valid_subset: bool


@dataclass(frozen=True)
class Track:
    track_id: str
    source_path: str
    file: str
    duration_s: float
    bytes: int


@dataclass
class Dataset:
    captions: list[Caption]
    tracks: dict[str, Track] = field(default_factory=dict)

    @property
    def by_track(self) -> dict[str, list[Caption]]:
        grouped: dict[str, list[Caption]] = {}
        for caption in self.captions:
            grouped.setdefault(caption.track_id, []).append(caption)
        return grouped


def md5(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(name: str, layout: Layout, log=print) -> Path:
    """Download ``name`` from the Zenodo record unless it is already here and
    intact. Raises when the checksum does not match the published one."""
    dest = layout.dataset / name
    expected = FILES[name]
    if dest.exists():
        actual = md5(dest)
        if actual == expected:
            log(f"{name}: cached, md5 ok")
            return dest
        log(f"{name}: md5 {actual} != {expected}, refetching")
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"{name}: downloading")
    urllib.request.urlretrieve(f"{_BASE}/{name}?download=1", dest)
    actual = md5(dest)
    if actual != expected:
        raise RuntimeError(f"{name}: md5 {actual} != published {expected}")
    log(f"{name}: downloaded, md5 ok")
    return dest


def read_captions(csv_path: Path) -> list[Caption]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [
            Caption(
                caption_id=row["caption_id"],
                track_id=row["track_id"],
                text=row["caption"],
                is_valid_subset=row["is_valid_subset"] == "True",
            )
            for row in csv.DictReader(handle)
        ]


def caption_stats(captions: Iterable[Caption]) -> dict:
    captions = list(captions)
    per_track = Counter(c.track_id for c in captions)
    words = [len(c.text.split()) for c in captions]
    return {
        "captions": len(captions),
        "tracks": len(per_track),
        "captions_per_track": dict(sorted(Counter(per_track.values()).items())),
        "valid_subset_captions": sum(1 for c in captions if c.is_valid_subset),
        "valid_subset_tracks": len(
            {c.track_id for c in captions if c.is_valid_subset}
        ),
        "non_ascii_captions": sum(1 for c in captions if not c.text.isascii()),
        "caption_words_min": min(words),
        "caption_words_median": statistics.median(words),
        "caption_words_max": max(words),
        "caption_chars_max": max(len(c.text) for c in captions),
    }


def _strip_tags(path: Path) -> None:
    """Leave the audio and nothing else.

    Two passes, because one is not enough: mutagen deletes the frames but
    leaves an empty ID3 container behind, and a container is still a place a
    tag could be. The second pass copies the file without its ID3v2 block or
    ID3v1 trailer, so a tag reader finds no tag at all rather than an empty
    one. Audio frames are copied byte for byte — nothing is re-encoded.
    """
    import mutagen

    audio = mutagen.File(path)
    if audio is not None and audio.tags is not None:
        audio.delete()
        audio.save()
    _strip_containers(path)


def _id3v2_length(header: bytes) -> int:
    """Bytes occupied by an ID3v2 tag whose 10-byte header this is."""
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = 10 if header[5] & 0x10 else 0
    return 10 + size + footer


def _strip_containers(path: Path) -> None:
    data = path.read_bytes()
    start = 0
    while data[start : start + 3] == b"ID3":
        start += _id3v2_length(data[start : start + 10])
    end = len(data)
    if end >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    if start or end != len(data):
        path.write_bytes(data[start:end])


def tag_residue(path: Path) -> list[str]:
    """What a tag reader can still find in ``path``. Empty means clean."""
    import mutagen

    found: list[str] = []
    audio = mutagen.File(path)
    if audio is not None and audio.tags:
        found.append(f"mutagen:{len(audio.tags)} frame(s)")
    with path.open("rb") as handle:
        if handle.read(3) == b"ID3":
            found.append("ID3v2 header")
        handle.seek(-128, 2)
        if handle.read(3) == b"TAG":
            found.append("ID3v1 trailer")
    return found


def prepare_audio(layout: Layout, log=print, force: bool = False) -> list[Track]:
    """Extract every captioned track into a flat, tagless, opaquely named
    library. Returns the tracks in manifest order.

    The caption table is read again here rather than taken from ``Dataset``:
    the archive member a track lives in is named by the table's ``path``
    column, which is about the dataset's layout and not about a caption.
    """
    import mutagen

    wanted = {}
    with layout.captions_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            wanted[row["path"]] = row["track_id"]

    audio_dir = layout.audio
    if force and audio_dir.exists():
        shutil.rmtree(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)

    tracks: list[Track] = []
    with zipfile.ZipFile(layout.audio_zip) as archive:
        # The archive names its files ``audio/<bucket>/<id>.2min.mp3`` while
        # the caption table names them ``<bucket>/<id>.mp3``; the id is what
        # both agree on.
        members = {}
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            folder, _, filename = name.rpartition("/")
            bucket = folder.rsplit("/", 1)[-1]
            members[f"{bucket}/{filename.split('.')[0]}.mp3"] = name
        missing = sorted(set(wanted) - set(members))
        if missing:
            raise RuntimeError(f"{len(missing)} captioned file(s) absent from audio.zip")
        for source_path, track_id in sorted(wanted.items(), key=lambda kv: kv[1]):
            target = audio_dir / opaque_name(track_id)
            if not target.exists():
                with archive.open(members[source_path]) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                _strip_tags(target)
            elif tag_residue(target):
                _strip_tags(target)
            info = mutagen.File(target).info
            tracks.append(
                Track(
                    track_id=track_id,
                    source_path=source_path,
                    file=target.name,
                    duration_s=round(info.length, 2),
                    bytes=target.stat().st_size,
                )
            )
    log(f"library: {len(tracks)} file(s) in {audio_dir}")
    return tracks


def leakage_check(tracks: list[Track], dataset: Dataset, layout: Layout) -> dict:
    """Prove the library carries no text: no tags anywhere, and no filename
    that shares a word with any caption."""
    dirty = {t.file: tag_residue(layout.audio / t.file) for t in tracks}
    dirty = {name: residue for name, residue in dirty.items() if residue}
    unreadable = sorted(t.file for t in tracks if not _decodable(layout.audio / t.file))
    names = {Path(t.file).stem for t in tracks}
    caption_words = {
        word.strip(".,!?()\"'").lower()
        for caption in dataset.captions
        for word in caption.text.split()
        if len(word) > 3
    }
    collisions = sorted(names & caption_words)
    extra = sorted(p.name for p in layout.audio.iterdir() if p.name not in {t.file for t in tracks})
    return {
        "files_checked": len(tracks),
        "files_with_tags": dirty,
        "files_libsndfile_cannot_read": unreadable,
        "filename_caption_word_collisions": collisions,
        "unexpected_files_in_library": extra,
        "clean": not dirty and not collisions and not extra and not unreadable,
    }


def _decodable(path: Path) -> bool:
    """What the embedder needs of a file: that libsndfile can open and read it."""
    import soundfile as sf

    try:
        with sf.SoundFile(path) as handle:
            return handle.read(1024, dtype="float32").size > 0
    except Exception:
        return False


def write_manifest(tracks: list[Track], dataset: Dataset, layout: Layout) -> None:
    per_track = dataset.by_track
    with layout.manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["track_id", "file", "duration_s", "bytes", "n_captions",
             "source_path", "kalinka_track_id"]
        )
        for track in tracks:
            writer.writerow([
                track.track_id,
                track.file,
                track.duration_s,
                track.bytes,
                len(per_track.get(track.track_id, [])),
                track.source_path,
                kalinka_track_id(layout.audio / track.file),
            ])


def load_manifest(layout: Layout) -> dict[str, Track]:
    with layout.manifest.open(newline="", encoding="utf-8") as handle:
        return {
            row["track_id"]: Track(
                track_id=row["track_id"],
                source_path=row["source_path"],
                file=row["file"],
                duration_s=float(row["duration_s"]),
                bytes=int(row["bytes"]),
            )
            for row in csv.DictReader(handle)
        }


def prepare(layout: Layout, log=print, force: bool = False) -> dict:
    """Stage 1 end to end: fetch, verify, strip, name, manifest, prove."""
    for name in FILES:
        fetch(name, layout, log)
    captions = read_captions(layout.captions_csv)
    dataset = Dataset(captions=captions)
    tracks = prepare_audio(layout, log, force=force)
    dataset.tracks = {t.track_id: t for t in tracks}
    write_manifest(tracks, dataset, layout)
    stats = caption_stats(captions)
    stats["audio_files"] = len(tracks)
    stats["audio_duration_s_total"] = round(sum(t.duration_s for t in tracks), 1)
    stats["audio_duration_s_median"] = round(
        statistics.median([t.duration_s for t in tracks]), 1
    )
    stats["leakage_check"] = leakage_check(tracks, dataset, layout)
    layout.dataset_stats.write_text(json.dumps(stats, indent=2))
    log(f"dataset: {stats['captions']} captions over {stats['tracks']} tracks")
    if not stats["leakage_check"]["clean"]:
        raise RuntimeError(f"leakage check failed: {stats['leakage_check']}")
    return stats
