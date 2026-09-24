"""What a plugin answers about its own configuration.

Two questions the server asks on behalf of the settings page. *What could
this field hold?* — :class:`ConfigOption`, offered as suggestions beside a
text field the user may still type into freely. *Is this value usable?* —
:class:`ConfigIssue`, the plugin's verdict on values the user has staged but
not yet saved.

Both are answered by :class:`~kalinka_plugin_sdk.plugin.PluginBase`, which
documents when each is called and what a plugin may do inside them.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ConfigOption(BaseModel):
    """One value a field could hold, offered to the user as a suggestion.

    @param value What gets written to the config when the user picks it.
    @param label What the user reads. Equal to ``value`` where the value is
        already the clearest thing to show.
    @param description A dim second line, for what distinguishes this option
        from its neighbours — an address behind a hostname, a filesystem
        behind a mount point.
    """

    model_config = ConfigDict(frozen=True)

    value: str
    label: str
    description: Optional[str] = None


class IssueSeverity(str, Enum):
    """How much a :class:`ConfigIssue` stands in the way.

    ERROR blocks the save: the value cannot be used as written and only the
    user can fix it. WARNING is shown beside the field and saved anyway —
    a share that is switched off is still worth configuring.
    """

    ERROR = "error"
    WARNING = "warning"


class ConfigIssue(BaseModel):
    """Something wrong with a configuration value, in words a user can act on.

    @param path The field, relative to the plugin's own config model
        (``music_folders``, ``enricher.plugins.acoustid.api_key``). The
        server prefixes it. An entry of a
        :data:`~kalinka_plugin_sdk.config_records.Records` collection is
        named by its id, and a part of it by the path within it:
        ``music_sources.<id>.location.host``.
    @param message Shown under the field. Says what to write instead, not
        what the code found.
    @param index Which item of a list field the issue is about; None when it
        is about the field as a whole.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    index: Optional[int] = Field(default=None, ge=0)
