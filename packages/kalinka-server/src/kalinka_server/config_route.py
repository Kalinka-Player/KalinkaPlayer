"""``/server/config``: the settings page's layout, its values, and writes to
them.

A write is judged whole before any of it is kept (:mod:`config_validation`),
and the dry run answers what the save would.
"""

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from kalinka_plugin_sdk import ConfigIssue

from .config_model import KalinkaConfig
from .config_overrides import save_overrides, store_override
from .config_schema_processor import (
    build_enum_options,
    build_presentation,
    build_static_values,
    build_values,
)
from .config_validation import (
    ConfigTargets,
    ConfigWriteError,
    blocking,
    changes_from_payload,
    commit_change,
    reconcile_written,
    stored_value,
    validate_changes,
)
from .player_setup import ModuleHealthState, PreparedModuleCollection

logger = logging.getLogger(__name__.split(".")[-1])


def register_config_routes(
    app: FastAPI, config: KalinkaConfig, modules: PreparedModuleCollection
) -> None:
    """Mount the configuration routes on ``app``.

    @param modules Read on every request, as its plugins can be set up again
        while the server runs.
    @note Reads ``schema_version``, ``dynamic_paths``,
        ``dynamic_field_registry``, ``options_registry``, ``overrides`` and
        ``overrides_file`` off ``app.state`` per request.
    """

    def _partition_modules_and_devices():
        successful_input_modules = {
            name: m.plugin_context.config
            for name, m in modules.prepared_input_modules.items()
            if m.health_state != ModuleHealthState.ERROR
        }
        failed_input_modules = {
            name: (m.plugin_context.config, m.error_message or "Unknown error")
            for name, m in modules.prepared_input_modules.items()
            if m.health_state == ModuleHealthState.ERROR
        }
        successful_devices = {
            name: d.plugin_context.config
            for name, d in modules.prepared_devices.items()
            if d.health_state != ModuleHealthState.ERROR
        }
        failed_devices = {
            name: (d.plugin_context.config, d.error_message or "Unknown error")
            for name, d in modules.prepared_devices.items()
            if d.health_state == ModuleHealthState.ERROR
        }
        return (
            successful_input_modules,
            failed_input_modules,
            successful_devices,
            failed_devices,
        )

    @app.get("/server/config/schema")
    def get_config_schema():
        ok_in, err_in, ok_dev, err_dev = _partition_modules_and_devices()
        schema = build_presentation(
            base_config=config,
            input_modules=ok_in,
            devices=ok_dev,
            input_modules_with_errors=err_in,
            devices_with_errors=err_dev,
            dynamic_field_registry=app.state.dynamic_field_registry,
        )
        return schema.model_dump(mode="json")

    @app.get("/server/config")
    async def get_config():
        ok_in, _err_in, ok_dev, _err_dev = _partition_modules_and_devices()
        known = await build_values(
            base_config=config,
            input_modules=ok_in,
            devices=ok_dev,
            dynamic_entries=app.state.dynamic_field_registry.values(),
        )
        # Resolve dynamic-options enums (ALSA devices etc.) per-request
        # so hot-plug is reflected without a schema bump. Each entry is
        # a list of {value, label} option specs.
        enum_options = await build_enum_options(app.state.options_registry)
        return {
            "schema_version": app.state.schema_version,
            "values": known.values,
            "secrets_set": sorted(known.secrets_set),
            "enum_options": {
                path: [opt.model_dump(mode="json") for opt in opts]
                for path, opts in enum_options.items()
            },
        }

    def _config_targets() -> ConfigTargets:
        return ConfigTargets(
            base_config=config,
            input_modules=modules.prepared_input_modules,
            devices=modules.prepared_devices,
        )

    async def _judge(
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], list[ConfigIssue]]:
        """The changes in a config write body and everything wrong with them.

        The dry run and the save ask exactly this, so neither can accept
        what the other would refuse.
        """
        try:
            changes = changes_from_payload(
                payload, app.state.schema_version, app.state.dynamic_paths
            )
            return changes, await validate_changes(changes, _config_targets())
        except ConfigWriteError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/server/config/validate")
    async def validate_config_fields(payload: Dict[str, Any]):
        """Judge staged changes without keeping any of them.

        Body is the one `PUT /server/config` takes. The answer is what the
        save would say, so the settings page can show it while the user is
        still typing.
        """
        _changes, issues = await _judge(payload)
        return {"issues": [issue.model_dump(mode="json") for issue in issues]}

    @app.put("/server/config")
    async def set_config_fields(payload: Dict[str, Any]):
        """Apply staged changes.

        Body: `{"schema_version": "...", "changes": {"<dotted.path>": value, ...}}`.
        Paths are relative to one of the three roots: `base_config.*`,
        `input_modules.<name>.*`, or `devices.<name>.*`. No `root.` or
        `.fields.` wrappers.

        Refused whole when any change draws an error, so a batch the user
        staged together never lands in halves. What was only warned about is
        applied and reported, because a client that saved without a dry run
        has nowhere else to learn of it.

        The answer names the credentials now set, as `GET /server/config`
        does, so a client can show which are saved without reading the whole
        configuration again.
        """
        changes, issues = await _judge(payload)
        refused = blocking(issues)
        if refused:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": refused[0].message,
                    "issues": [issue.model_dump(mode="json") for issue in issues],
                },
            )

        applied: Dict[str, Any] = {}
        committed = []
        targets = _config_targets()
        try:
            for key, value in changes.items():
                try:
                    target = targets.resolve(key)
                    reason = commit_change(target, key, value)
                except ConfigWriteError as exc:
                    logger.warning("Invalid config key '%s': %s", key, exc)
                    raise HTTPException(
                        status_code=exc.status_code, detail=str(exc)
                    ) from exc
                if reason is not None:
                    raise HTTPException(status_code=400, detail=reason)
                applied[key] = stored_value(target)
                committed.append(target)
        finally:
            # A batch that draws an error is refused above, before anything
            # is written. Past that point a change can still fail only for a
            # reason the check could not reach, and what already went into
            # memory cannot be taken back out — so it is persisted, and a
            # restart matches what is running.
            applied.update(reconcile_written(committed))
            if applied:
                for key, value in applied.items():
                    store_override(app.state.overrides, key, value)
                try:
                    save_overrides(app.state.overrides_file, app.state.overrides)
                except OSError as exc:
                    logger.error(
                        "Failed to persist overrides to %s: %s",
                        app.state.overrides_file,
                        exc,
                    )

        ok_in, _err_in, ok_dev, _err_dev = _partition_modules_and_devices()
        known = build_static_values(config, ok_in, ok_dev)
        return {
            "message": "Ok",
            "schema_version": app.state.schema_version,
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "secrets_set": sorted(known.secrets_set),
        }
