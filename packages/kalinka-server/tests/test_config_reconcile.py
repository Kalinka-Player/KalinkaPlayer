"""A setting another one mirrors, kept in agreement by its module.

Contract:

* A plugin's configuration reconciles after every write
  (``ModuleConfig.reconcile``): the dry run judges the candidate reconciled,
  a save reconciles the live configuration, and so does start-up once the
  overrides are applied. The plugin is told only what was written.
* Whatever the reconcile changed is kept as overrides beside what was
  written, so a restart reads both in agreement, and a record it made keeps
  its id instead of being made afresh on every start.
* The server's own configuration has nothing to reconcile.
* A whole value kept at a path drops what was kept beneath it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kalinka_plugin_sdk import ConfigRecord, Records
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginBase, PluginType
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix, store_override
from kalinka_server.config_validation import (
    ConfigTargets,
    commit_change,
    reconcile_written,
    stored_value,
    validate_changes,
)
from kalinka_server.player_setup import PreparedModuleCollection


class _Place(ConfigRecord):
    path: str = ""


class _Config(ModuleConfig):
    """``folders`` is ``places`` again, in the shape an older app edits."""

    name: str = Field(default="mod", frozen=True, exclude=True)
    folders: list[str] = Field(default_factory=list)
    places: Records[_Place] = Field(default_factory=list)
    port: int = 8000

    def reconcile(self, written: frozenset[str]) -> None:
        if "folders" in written:
            known = {place.path: place for place in self.places}
            self.places = [known.get(f) or _Place(path=f) for f in self.folders]
        self.folders = [place.path for place in self.places]


class _Plugin(PluginBase):
    PLUGIN_ID = "mod"
    REQUIRES_SDK = "1.0"
    PLUGIN_TYPE = PluginType.INPUT_MODULE
    CONFIG_MODEL = _Config

    async def setup(self, context):
        return None


class _Judge:
    """A plugin that remembers what it was asked to judge."""

    def __init__(self):
        self.asked: list[tuple[_Config, frozenset[str]]] = []

    async def validate_config(self, candidate, changed):
        self.asked.append((candidate, changed))
        return []


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


def _targets(config, plugin=None) -> ConfigTargets:
    return ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"mod": _Prepared(_Context(config), plugin)},
        devices={},
    )


def _live() -> _Config:
    return _Config(places=[{"id": "usb", "path": "/mnt/usb"}])


def _save(config: _Config, changes: dict[str, Any]) -> dict[str, Any]:
    """What ``PUT /server/config`` keeps in the overrides file."""
    targets = _targets(config)
    applied: dict[str, Any] = {}
    committed = []
    for key, value in changes.items():
        target = targets.resolve(key)
        assert commit_change(target, key, value) is None
        applied[key] = stored_value(target)
        committed.append(target)
    applied.update(reconcile_written(committed))
    return applied


class TestTheDryRun:
    def test_judges_the_candidate_reconciled(self):
        judge = _Judge()
        asyncio.run(
            validate_changes(
                {"input_modules.mod.folders": ["/mnt/usb", "/srv"]},
                _targets(_live(), judge),
            )
        )
        [(candidate, changed)] = judge.asked
        assert [p.path for p in candidate.places] == ["/mnt/usb", "/srv"]
        assert candidate.places[0].id == "usb"
        assert changed == {"folders"}

    def test_leaves_the_live_configuration_as_it_was(self):
        live = _live()
        asyncio.run(
            validate_changes(
                {"input_modules.mod.folders": ["/srv"]}, _targets(live, _Judge())
            )
        )
        assert live.folders == ["/mnt/usb"]
        assert [p.id for p in live.places] == ["usb"]


class TestASave:
    def test_keeps_what_the_reconcile_changed_beside_what_was_written(self):
        live = _live()
        kept = _save(live, {"input_modules.mod.folders": ["/mnt/usb", "/srv"]})

        assert kept.keys() == {"input_modules.mod.folders", "input_modules.mod.places"}
        restarted = _Config()
        apply_overrides_with_prefix(restarted, kept, "input_modules.mod.")
        assert restarted.places == live.places

    def test_of_the_other_shape_keeps_the_mirror_too(self):
        kept = _save(_live(), {"input_modules.mod.places": [{"id": "a", "path": "/a"}]})
        assert kept["input_modules.mod.folders"] == ["/a"]

    def test_that_touches_neither_keeps_only_itself(self):
        assert _save(_live(), {"input_modules.mod.port": 9000}).keys() == {
            "input_modules.mod.port"
        }

    def test_of_the_server_s_own_configuration_reconciles_nothing(self):
        target = _targets(_live()).resolve("base_config.server.port")
        assert reconcile_written([target]) == {}


class TestStartingUp:
    def _build(self, overrides):
        collection = PreparedModuleCollection()
        config = collection._build_module_config("mod", _Plugin, overrides)
        return collection, config

    def test_folders_saved_by_an_older_version_are_kept_as_places(self):
        overrides = {"input_modules.mod.folders": ["/mnt/usb"]}
        collection, config = self._build(overrides)

        [place] = config.places
        assert place.path == "/mnt/usb"
        assert overrides["input_modules.mod.places"] == [
            {"id": place.id, "path": "/mnt/usb"}
        ]
        assert collection.overrides_dirty

    def test_the_place_keeps_its_id_from_then_on(self):
        overrides = {"input_modules.mod.folders": ["/mnt/usb"]}
        _collection, first = self._build(overrides)
        collection, again = self._build(overrides)

        assert again.places == first.places
        assert not collection.overrides_dirty


def test_a_whole_value_drops_the_parts_kept_beneath_it():
    overrides = {"a.b.c": 1, "a.bc": 2, "a.b.d.e": 3}
    store_override(overrides, "a.b", {"c": 4})
    assert overrides == {"a.b": {"c": 4}, "a.bc": 2}
