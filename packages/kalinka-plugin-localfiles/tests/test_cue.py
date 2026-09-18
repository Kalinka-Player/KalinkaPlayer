#!/usr/bin/env python3
"""CUE-sheet parsing: disc + per-track metadata, encoding robustness, and
sibling-cue discovery. Modelled on a real single-file CD rip (Roxy Music -
Avalon) whose .cue is UTF-16.
"""

from kalinka_plugin_localfiles.indexer.cue import find_cue_for, parse_cue
from kalinka_plugin_localfiles.storage.local import LocalStorage

LOCAL = LocalStorage()

CUE = """REM GENRE "Pop Rock, Synth-pop"
REM DATE "1982"
REM COMMENT "EG - EGHP 50, UK, Vinyl, LP, Album"
PERFORMER "Roxy Music"
TITLE "Avalon"
FILE "1982 - Roxy Music - Avalon.flac" WAVE
  TRACK 01 AUDIO
    TITLE "More Than This"
    PERFORMER "Roxy Music"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "The Space Between"
    PERFORMER "Roxy Music"
    INDEX 00 04:28:00
    INDEX 01 04:30:00
  TRACK 03 AUDIO
    TITLE "Avalon"
    PERFORMER "Roxy Music"
    INDEX 01 08:58:00
"""


def _write(path, text, encoding):
    path.write_bytes(text.encode(encoding))
    return str(path)


def test_disc_level_metadata(tmp_path):
    p = _write(tmp_path / "a.cue", CUE, "utf-8")
    sheet = parse_cue(LOCAL, p)
    assert sheet.performer == "Roxy Music"
    assert sheet.title == "Avalon"
    assert sheet.genre == "Pop Rock, Synth-pop"
    assert sheet.date == "1982"


def test_per_track_titles_and_offsets(tmp_path):
    p = _write(tmp_path / "a.cue", CUE, "utf-8")
    tracks = parse_cue(LOCAL, p).files[0].tracks
    assert [t.number for t in tracks] == [1, 2, 3]
    assert [t.title for t in tracks] == [
        "More Than This",
        "The Space Between",
        "Avalon",
    ]
    # INDEX 01 is the start; INDEX 00 pre-gap is ignored.
    assert tracks[0].start_seconds == 0.0
    assert tracks[1].start_seconds == 4 * 60 + 30
    assert tracks[2].start_seconds == 8 * 60 + 58


def test_utf16_encoding(tmp_path):
    # The real Avalon.cue is UTF-16; decoding must not fall back to mojibake.
    p = _write(tmp_path / "a.cue", CUE, "utf-16")
    sheet = parse_cue(LOCAL, p)
    assert sheet.performer == "Roxy Music"
    assert sheet.files[0].tracks[1].title == "The Space Between"


def test_cyrillic_cp1251(tmp_path):
    cyr = 'PERFORMER "Кино"\nTITLE "Группа крови"\nFILE "x.flac" WAVE\n' \
          '  TRACK 01 AUDIO\n    TITLE "Война"\n    INDEX 01 00:00:00\n'
    p = _write(tmp_path / "a.cue", cyr, "cp1251")
    sheet = parse_cue(LOCAL, p)
    assert sheet.performer == "Кино"
    assert sheet.files[0].tracks[0].title == "Война"


def test_tracks_for_single_file(tmp_path):
    p = _write(tmp_path / "a.cue", CUE, "utf-8")
    sheet = parse_cue(LOCAL, p)
    # Exact filename match and the single-file implicit match both work.
    assert len(sheet.tracks_for("1982 - Roxy Music - Avalon.flac")) == 3
    assert len(sheet.tracks_for("anything-else.flac")) == 3


def test_no_tracks_returns_none(tmp_path):
    p = _write(tmp_path / "a.cue", 'PERFORMER "X"\nTITLE "Y"\n', "utf-8")
    assert parse_cue(LOCAL, p) is None


def test_find_cue_same_stem(tmp_path):
    audio = tmp_path / "1982 - Roxy Music - Avalon.flac"
    audio.write_bytes(b"x")
    _write(tmp_path / "1982 - Roxy Music - Avalon.cue", CUE, "utf-8")
    assert find_cue_for(LOCAL, str(audio)) == str(
        tmp_path / "1982 - Roxy Music - Avalon.cue"
    )


def test_find_cue_by_file_reference(tmp_path):
    # Cue named differently from the audio, but its FILE line points at it.
    audio = tmp_path / "1982 - Roxy Music - Avalon.flac"
    audio.write_bytes(b"x")
    _write(tmp_path / "disc.cue", CUE, "utf-8")
    assert find_cue_for(LOCAL, str(audio)) == str(tmp_path / "disc.cue")


def test_find_cue_none_when_absent(tmp_path):
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"x")
    assert find_cue_for(LOCAL, str(audio)) is None
