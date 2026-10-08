"""Per-renderer preferences: which renderer plays, and what controls its volume.

Deliberately not part of the config-overrides file. Entries are keyed by
renderer ids that appear and vanish at runtime, so they would churn the config
schema; writes must take effect immediately, without a ``schema_version``
handshake or a module restart; and the values are wiring, not module settings.
The same reasoning already keeps renderer settings out of ``/server/config``
(see :mod:`renderer_config`).

A store built without a path keeps everything in memory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__.split(".")[-1])


class RendererPreferences:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._selected_renderer_id: Optional[str] = None
        self._selected_renderer_name: Optional[str] = None
        self._renderers: dict[str, dict[str, Any]] = {}
        if path:
            self._load()

    # ---------------------------------------------------------------- reading
    @property
    def selected_renderer_id(self) -> Optional[str]:
        return self._selected_renderer_id

    @property
    def selected_renderer_name(self) -> Optional[str]:
        """What the selected renderer last called itself; None if never heard."""
        return self._selected_renderer_name

    def volume_control(self, renderer_id: str) -> Optional[str]:
        """Plugin id of the module that owns this renderer's volume, or None
        when the renderer controls its own."""
        return self._renderers.get(renderer_id, {}).get("volume_control")

    def to_dict(self) -> dict:
        return {
            "selected_renderer_id": self._selected_renderer_id,
            "selected_renderer_name": self._selected_renderer_name,
            "renderers": {k: dict(v) for k, v in self._renderers.items()},
        }

    # ---------------------------------------------------------------- writing
    def set_selected(
        self, renderer_id: Optional[str], name: Optional[str] = None
    ) -> None:
        if renderer_id is None:
            name = None
        if (renderer_id, name) == (
            self._selected_renderer_id,
            self._selected_renderer_name,
        ):
            return
        self._selected_renderer_id = renderer_id
        self._selected_renderer_name = name
        self._save()

    def set_volume_control(self, renderer_id: str, module: Optional[str]) -> None:
        if self.volume_control(renderer_id) == module:
            return
        entry = self._renderers.setdefault(renderer_id, {})
        if module is None:
            entry.pop("volume_control", None)
        else:
            entry["volume_control"] = module
        if not entry:
            del self._renderers[renderer_id]
        self._save()

    # -------------------------------------------------------------- on disk
    def _load(self) -> None:
        assert self._path is not None
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Cannot read renderer preferences %s: %s. Starting empty.",
                self._path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning("Ignoring malformed renderer preferences %s", self._path)
            return
        selected = data.get("selected_renderer_id")
        self._selected_renderer_id = selected if isinstance(selected, str) else None
        name = data.get("selected_renderer_name")
        self._selected_renderer_name = (
            name if isinstance(name, str) and self._selected_renderer_id else None
        )
        renderers = data.get("renderers")
        if isinstance(renderers, dict):
            self._renderers = {
                key: dict(value)
                for key, value in renderers.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    def _save(self) -> None:
        if not self._path:
            return
        state_dir = os.path.dirname(self._path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".renderers-", suffix=".tmp", dir=state_dir or "."
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("Failed to persist renderer preferences: %s", exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
