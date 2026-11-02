"""Tests for one-shot config triggers.

Covers detection (``is_one_shot_field`` / ``find_one_shot_overrides``) and the
persist-first consume on ``PreparedModuleCollection`` — the guarantee that an
armed "do X on next restart" trigger is reset *before* the plugin acts, so it
fires at most once and can never re-fire on the following boot.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginType

from kalinka_server.config_overrides import (
    find_one_shot_overrides,
    is_one_shot_field,
    load_overrides,
)
from kalinka_server.player_setup import PreparedModuleCollection


class _Nested(BaseModel):
    flag: bool = Field(default=False, json_schema_extra={"one_shot": True})


class _Cfg(ModuleConfig):
    rebuild: bool = Field(default=False, json_schema_extra={"one_shot": True})
    plain: bool = Field(default=False)
    sub: _Nested = Field(default_factory=_Nested)


class _Plugin:
    PLUGIN_TYPE = PluginType.INPUT_MODULE
    CONFIG_MODEL = _Cfg


PREFIX = "input_modules.fake."


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_is_one_shot_field_top_level():
    assert is_one_shot_field(_Cfg, ["rebuild"]) is True
    assert is_one_shot_field(_Cfg, ["plain"]) is False


def test_is_one_shot_field_nested():
    assert is_one_shot_field(_Cfg, ["sub", "flag"]) is True


def test_is_one_shot_field_unknown_path():
    assert is_one_shot_field(_Cfg, ["nope"]) is False
    assert is_one_shot_field(_Cfg, ["sub", "nope"]) is False


def test_find_one_shot_overrides_returns_only_armed_one_shots():
    cfg = _Cfg(rebuild=True, plain=True)
    overrides = {
        PREFIX + "rebuild": True,  # one-shot, armed
        PREFIX + "plain": True,  # not one-shot
        "devices.x.rebuild": True,  # wrong prefix
    }
    assert find_one_shot_overrides(_Cfg, PREFIX, cfg, overrides) == [
        PREFIX + "rebuild"
    ]


def test_find_one_shot_overrides_not_armed_when_value_is_default():
    cfg = _Cfg(rebuild=False)
    overrides = {PREFIX + "rebuild": False}
    assert find_one_shot_overrides(_Cfg, PREFIX, cfg, overrides) == []


# ---------------------------------------------------------------------------
# Consume (persist-first)
# ---------------------------------------------------------------------------


def _collection(overrides_file=None) -> PreparedModuleCollection:
    c = PreparedModuleCollection()
    c.overrides_file = overrides_file
    return c


def test_consume_resets_persist_first_and_keeps_armed_value(tmp_path):
    ofile = str(tmp_path / "ov.json")
    overrides = {PREFIX + "rebuild": True, PREFIX + "plain": True}
    with open(ofile, "w") as f:
        json.dump(overrides, f)

    cfg = _Cfg(rebuild=True, plain=True)
    live = dict(overrides)
    consumed = _collection(ofile)._consume_one_shot_overrides(
        "fake", _Plugin, cfg, live
    )

    # The consumed key is reported so the caller can reset the loaded value.
    assert consumed == [PREFIX + "rebuild"]
    # Trigger dropped from the live dict and from disk...
    assert PREFIX + "rebuild" not in live
    assert PREFIX + "rebuild" not in load_overrides(ofile)
    # ...other overrides untouched...
    assert load_overrides(ofile).get(PREFIX + "plain") is True
    # ...but the armed value stays on config so the plugin acts this boot.
    assert cfg.rebuild is True


def test_consume_is_noop_when_nothing_armed(tmp_path):
    ofile = str(tmp_path / "ov.json")
    overrides = {PREFIX + "plain": True}
    with open(ofile, "w") as f:
        json.dump(overrides, f)

    cfg = _Cfg(plain=True)
    live = dict(overrides)
    _collection(ofile)._consume_one_shot_overrides("fake", _Plugin, cfg, live)

    assert live == {PREFIX + "plain": True}


def test_consume_disarms_in_memory_when_persist_fails(monkeypatch):
    """If the reset can't be persisted, the trigger must NOT fire: the value
    is disarmed in memory and left armed on disk for a retry next boot."""
    import kalinka_server.player_setup as ps

    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(ps, "save_overrides", boom)

    cfg = _Cfg(rebuild=True)
    live = {PREFIX + "rebuild": True}
    consumed = _collection("/does/not/matter")._consume_one_shot_overrides(
        "fake", _Plugin, cfg, live
    )

    # Override left in place (retried next boot) and config disarmed so the
    # plugin skips the action this boot — never act without a durable reset.
    assert PREFIX + "rebuild" in live
    assert cfg.rebuild is False
    # Nothing durably consumed, so the caller has nothing to reset.
    assert consumed == []


def test_consume_without_file_clears_in_memory(tmp_path):
    cfg = _Cfg(rebuild=True)
    live = {PREFIX + "rebuild": True}
    _collection(None)._consume_one_shot_overrides("fake", _Plugin, cfg, live)
    assert PREFIX + "rebuild" not in live
    assert cfg.rebuild is True


# ---------------------------------------------------------------------------
# Reset the loaded value after consumption
# ---------------------------------------------------------------------------


def test_reset_consumed_one_shot_restores_default_on_loaded_config():
    """After the plugin has acted, the loaded value must go back to default so
    GET /server/config reports the disarmed state — not the armed value that
    lingered on the in-memory config until the next restart (the reported bug).
    """
    cfg = _Cfg(rebuild=True)  # armed value still on config post-setup
    PreparedModuleCollection()._reset_consumed_one_shot_values(
        "fake", _Plugin, cfg, [PREFIX + "rebuild"]
    )
    assert cfg.rebuild is False


def test_reset_consumed_one_shot_handles_nested_field():
    cfg = _Cfg()
    cfg.sub.flag = True
    PreparedModuleCollection()._reset_consumed_one_shot_values(
        "fake", _Plugin, cfg, [PREFIX + "sub.flag"]
    )
    assert cfg.sub.flag is False


def test_reset_consumed_one_shot_noop_without_consumed_keys():
    cfg = _Cfg(rebuild=True)
    PreparedModuleCollection()._reset_consumed_one_shot_values(
        "fake", _Plugin, cfg, []
    )
    # No keys consumed (e.g. disabled module / persist failure) → left as-is.
    assert cfg.rebuild is True


def test_reconcile_never_touches_one_shot_fields():
    """One-shot triggers are owned by the consume step. Reconcile (which runs
    after setup) must leave them alone — otherwise a transient persist failure
    that disarmed the in-memory value would let reconcile drop the still-armed
    override, silently losing an un-fired trigger.
    """
    # Disarmed in memory (rebuild=False) but the override is still on disk —
    # the exact post-persist-failure state. Reconcile must NOT drop it.
    cfg = _Cfg(rebuild=False)
    overrides = {PREFIX + "rebuild": True}
    changed = PreparedModuleCollection()._reconcile_consumed_overrides(
        "fake", _Plugin, cfg, overrides
    )
    assert changed == 0
    assert overrides == {PREFIX + "rebuild": True}


def test_is_one_shot_field_walks_optional_nested_model():
    from typing import Optional

    class _OptNested(BaseModel):
        flag: bool = Field(default=False, json_schema_extra={"one_shot": True})

    class _OptCfg(ModuleConfig):
        sub: Optional[_OptNested] = None

    assert is_one_shot_field(_OptCfg, ["sub", "flag"]) is True


def test_immediate_actions_reset_durably_without_consuming_restart_actions(tmp_path):
    from types import SimpleNamespace
    from kalinka_plugin_sdk.module_health import ModuleHealthState

    class Immediate(_Cfg):
        pair: bool = Field(
            default=False,
            json_schema_extra={"one_shot": True, "apply_immediately": True},
        )

    class Plugin(_Plugin):
        CONFIG_MODEL = Immediate

    config = Immediate(name="fake", pair=True, rebuild=True)
    prepared = SimpleNamespace(
        plugin_class=Plugin,
        plugin_context=SimpleNamespace(config=config),
        health_state=ModuleHealthState.READY,
    )
    overrides = {PREFIX + "pair": True, PREFIX + "rebuild": True}
    collection = _collection(str(tmp_path / "overrides.json"))
    consumed = collection.consume_immediate_actions(
        prepared, {"pair", "rebuild"}, overrides
    )
    assert consumed == [PREFIX + "pair"]
    assert config.pair and config.rebuild  # Callback still sees the action.
    assert load_overrides(collection.overrides_file) == {PREFIX + "rebuild": True}
    collection.finish_immediate_actions(prepared, consumed)
    assert not config.pair and config.rebuild

    config.pair = True
    overrides[PREFIX + "pair"] = True
    prepared.health_state = ModuleHealthState.DISABLED
    assert collection.consume_immediate_actions(prepared, {"pair"}, overrides) == []
    assert config.pair and overrides[PREFIX + "pair"]


def test_immediate_action_detection_requires_both_flags_and_handles_nested_models():
    from kalinka_server.config_overrides import is_immediate_action_field

    class Actions(BaseModel):
        pair: bool = Field(
            default=False,
            json_schema_extra={"one_shot": True, "apply_immediately": True},
        )
        ordinary: bool = Field(
            default=False, json_schema_extra={"apply_immediately": True}
        )

    class Config(_Cfg):
        actions: Actions | None = None

    assert is_immediate_action_field(Config, ["actions", "pair"])
    assert not is_immediate_action_field(Config, ["actions", "ordinary"])
    assert not is_immediate_action_field(Config, ["rebuild"])
    assert not is_immediate_action_field(Config, ["unknown"])
