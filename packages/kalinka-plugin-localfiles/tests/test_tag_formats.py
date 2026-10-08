"""Every file the indexer takes in has one way to be read, which the
metadata and the cover share."""

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.indexer.indexer import (
    SUPPORTED_AUDIO_EXTENSIONS,
    FileIndexer,
)


@pytest.mark.parametrize("extension", sorted(SUPPORTED_AUDIO_EXTENSIONS))
def test_an_accepted_file_has_a_reader(tmp_path, extension):
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)

    _, tag_format = fi._tag_format_of(f"smb://nas/music/Track #1{extension}")

    assert tag_format is not None


def test_a_file_of_no_known_type_has_none(tmp_path):
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)

    assert fi._tag_format_of("/music/notes.txt") == ("text/plain", None)
