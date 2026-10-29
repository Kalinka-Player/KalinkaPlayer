"""Plugin-reportable health state.

Plugins report their current state via `get_state()`, which the server
calls on demand (e.g. when /server/modules is fetched). Internal
sub-features (subprocesses, optional pipelines) are an implementation
detail of each plugin — they aggregate into one of these states plus a
human-readable message.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ModuleHealthState(str, Enum):
    """Roll-up health state a plugin advertises to the server."""

    READY = "ready"
    WARNING = "warning"
    ERROR = "error"
    DISABLED = "disabled"


class ModuleState(BaseModel):
    """Plugin-reported snapshot.

    `missing_packages` lists keys from the plugin's ``OPTIONAL_PACKAGES``
    that the plugin currently needs but cannot import. The server passes
    this list to the UI so it can offer a one-click "install and restart"
    affordance — the same keys are accepted by
    ``PUT /server/restart {"install": [...]}``.
    """

    state: ModuleHealthState
    message: Optional[str] = None
    missing_packages: list[str] = Field(default_factory=list)
