"""A resource fork macOS writes beside a track is not indexed as one."""

from kalinka_plugin_localfiles.indexer.indexer import is_supported_audio_file


def test_a_resource_fork_is_not_audio():
    assert not is_supported_audio_file("smb://nas/music/Bach/._01 Aria.flac")
    assert is_supported_audio_file("smb://nas/music/Bach/01 Aria.flac")
