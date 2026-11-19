"""A library the size a long-time user has, without their music.

Thousands of short tagged FLAC files laid out as albums, which the real
indexer then indexes, and a way to mark the result as a settled library:
enriched and CLAP-embedded, as one is after its first days. Enriching and
embedding that many tracks for real would take a day and tens of thousands
of MusicBrainz requests.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

import numpy as np
import soundfile
from kalinka_plugin_localfiles.embedding_utils import CLAP_MODEL_VERSION
from mutagen.flac import FLAC

TRACKS_PER_ALBUM = 12
ALBUMS_PER_ARTIST = 3

_SAMPLE_RATE = 44100
_SECONDS = 2


def _template(directory: Path) -> Path:
    """Two seconds of silence, which FLAC stores in a few hundred bytes:
    nothing listens to these tracks, and there are thousands of them."""
    path = directory / "synthetic-template.flac"
    if not path.exists():
        silence = np.zeros((_SAMPLE_RATE * _SECONDS, 2), dtype="float32")
        soundfile.write(str(path), silence, _SAMPLE_RATE, format="FLAC")
    return path


def plant_synthetic_library(root: Path, tracks: int, scratch: Path) -> None:
    """Fill ``root`` with ``tracks`` tagged files, twelve to an album and
    three albums to an artist.

    Their mtime is set two hours back: the indexer holds a file written
    moments ago until it has stopped changing.
    """
    template = _template(scratch)
    written_at = time.time() - 7200
    planted = 0
    album = 0
    while planted < tracks:
        artist_name = f"Synthetic Artist {album // ALBUMS_PER_ARTIST:04d}"
        album_name = f"Synthetic Album {album:04d}"
        folder = root / artist_name / album_name
        folder.mkdir(parents=True, exist_ok=True)
        for number in range(1, min(TRACKS_PER_ALBUM, tracks - planted) + 1):
            title = f"Song {album:04d}-{number:02d}"
            path = folder / f"{number:02d} {title}.flac"
            shutil.copyfile(template, path)
            tags = FLAC(path)
            tags["artist"] = artist_name
            tags["albumartist"] = artist_name
            tags["album"] = album_name
            tags["title"] = title
            tags["tracknumber"] = str(number)
            tags.save()
            os.utime(path, (written_at, written_at))
            planted += 1
        album += 1


def mark_settled(db_path: Path) -> None:
    """Mark every indexed artist, album and track enriched, and every track
    embedded by both CLAP stages. The server must be stopped."""
    conn = sqlite3.connect(db_path)
    try:
        for table in ("artists", "albums", "tracks"):
            conn.execute(f"UPDATE {table} SET enriched = 1")
        conn.execute(
            """
            INSERT OR IGNORE INTO embedding_jobs
                (entity_type, entity_id, stage, status, model_version)
            SELECT 'track', t.id, s.stage, 'done', ?
            FROM tracks t,
                 (SELECT 'clap_audio' AS stage UNION ALL SELECT 'clap_text') s
            """,
            (CLAP_MODEL_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
