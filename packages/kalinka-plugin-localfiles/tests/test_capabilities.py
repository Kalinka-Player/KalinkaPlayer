"""AI search is reported only while the setting gave the module a searcher."""

from kalinka_plugin_sdk.inputmodule import ModuleCapability

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule


def _module(tmp_path, queues):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return LocalFilesInputModule(config, None, *queues)


def test_reported_with_a_searcher(tmp_path):
    module = _module(tmp_path, (object(), object()))
    assert module.capabilities() == {ModuleCapability.AI_SEARCH}


def test_left_out_when_the_setting_is_off(tmp_path):
    assert _module(tmp_path, (None, None)).capabilities() == frozenset()
    assert _module(tmp_path, (object(), None)).capabilities() == frozenset()
