"""ai_search is advertised only while the setting gave the module a searcher."""

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule


def _module(tmp_path, queues):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return LocalFilesInputModule(config, None, *queues)


def test_available_with_a_searcher(tmp_path):
    assert _module(tmp_path, (object(), object())).ai_search_available()


def test_unavailable_when_the_setting_is_off(tmp_path):
    assert not _module(tmp_path, (None, None)).ai_search_available()
    assert not _module(tmp_path, (object(), None)).ai_search_available()
