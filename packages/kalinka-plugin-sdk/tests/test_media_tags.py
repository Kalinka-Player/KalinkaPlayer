"""Reading a track's tags and cover straight from its file."""

import io

import numpy as np
import pytest
import soundfile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from kalinka_plugin_sdk.media.folder_art import LocalFiles, find_folder_cover
from kalinka_plugin_sdk.media.formats import is_supported_audio_file, media_type_of
from kalinka_plugin_sdk.media.images import resize_to_jpeg
from kalinka_plugin_sdk.media.tags import (
    TagSummary,
    read_cover,
    read_summary,
    title_from_filename,
)


def _png(width=300, height=300, colour=(200, 30, 50)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def _flac(path, cover=None, **tags):
    soundfile.write(str(path), np.zeros(44100, dtype="int16"), 44100, format="FLAC")
    flac = FLAC(str(path))
    for key, value in tags.items():
        flac[key] = value
    if cover is not None:
        picture = Picture()
        picture.type = PictureType.COVER_FRONT
        picture.mime = "image/png"
        picture.data = cover
        flac.add_picture(picture)
    flac.save()
    return path


def test_a_tagged_flac_is_summarised(tmp_path):
    path = _flac(
        tmp_path / "01 So What.flac",
        cover=_png(),
        title="So What",
        artist="Miles Davis",
        album="Kind of Blue",
        albumartist="Miles Davis",
        tracknumber="1/5",
        discnumber="1",
        date="1959-08-17",
    )

    assert read_summary(str(path)) == TagSummary(
        title="So What",
        artist="Miles Davis",
        album="Kind of Blue",
        album_artist="Miles Davis",
        track_number=1,
        disc_number=1,
        year=1959,
        duration_s=pytest.approx(1.0, abs=0.01),
        codec="flac",
        has_cover=True,
    )
    assert read_cover(str(path)) == _png()


def test_an_untagged_file_has_an_empty_summary(tmp_path):
    path = tmp_path / "take.wav"
    soundfile.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="WAV")

    summary = read_summary(str(path))

    assert summary.title is None and summary.codec == "pcm" and not summary.has_cover
    assert read_cover(str(path)) is None


def test_a_file_that_is_not_what_its_name_says_is_skipped(tmp_path):
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not a flac at all")

    assert read_summary(str(path)) is None


def test_an_unknown_format_is_not_read(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")

    assert read_summary(str(path)) is None
    assert not is_supported_audio_file(str(path))


def test_formats_are_named_by_extension_alone():
    assert media_type_of("smb://nas/music/Symphony #5.flac") == "audio/flac"
    assert is_supported_audio_file("/media/kalinka/X/Kind of Blue/01.m4a")
    assert not is_supported_audio_file("/media/kalinka/X/._01.m4a")


@pytest.mark.parametrize(
    "name, title",
    [
        ("01 So What.flac", "So What"),
        ("01 - So What.flac", "So What"),
        ("03. Blue in Green.mp3", "Blue in Green"),
        ("So What.flac", "So What"),
        ("1999.flac", "1999"),
    ],
)
def test_a_title_falls_back_to_the_file_name(name, title):
    assert title_from_filename(f"/x/{name}") == title


def test_cover_art_is_resized_to_a_jpeg():
    jpeg = resize_to_jpeg(_png(1200, 1000), 230)

    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG" and max(image.size) == 230


def test_what_is_not_an_image_is_not_resized():
    assert resize_to_jpeg(b"nope", 230) is None


def test_a_folder_cover_is_found_on_the_local_filesystem(tmp_path):
    (tmp_path / "01.flac").write_bytes(b"")
    (tmp_path / "cover.png").write_bytes(_png())
    (tmp_path / "back.png").write_bytes(_png())

    found = find_folder_cover(LocalFiles(), str(tmp_path))

    assert found.path == str(tmp_path / "cover.png")
