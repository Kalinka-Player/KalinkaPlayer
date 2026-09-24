"""How a module is named and drawn wherever the app shows it.

A module's config names it (its ``name`` field's title) and may name a
material icon for it (``__module_icon__``). The settings card and the badge
on music from the module draw the same icon; a module that names none is
lettered instead.
"""

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation, module_icon


def _card():
    schema = build_presentation(
        KalinkaConfig(), {"localfiles": LocalFilesConfig()}, {}
    )
    return schema.pages[1].modules[0]


def test_the_library_is_called_my_library():
    assert _card().title == "My Library"


def test_the_library_is_drawn_as_a_folder():
    assert module_icon(LocalFilesConfig) == "folder_outlined"


def test_the_settings_card_draws_the_icon_the_badge_does():
    assert _card().icon == module_icon(LocalFilesConfig)


def test_a_module_that_names_no_icon_has_none():
    assert module_icon(ModuleConfig) is None
