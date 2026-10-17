"""What a source can answer beyond the calls every source answers.

Browsing and matching a name are what make a source a source, so they are
not named here — only the optional halves are. A client reads the list to
decide which requests to make at all, rather than making one and learning
from an empty answer that it never needed to ask.

Each capability names one call, not a category. "Suggestions" would cover
both a semantic search and a recommendation drawn from what has been played,
and a source may well have one without the other.
"""

from __future__ import annotations

import logging
from typing import Any, List

from kalinka_plugin_sdk.inputmodule import InputModule, ModuleCapability

logger = logging.getLogger(__name__)


def capabilities_of(interface: Any) -> List[str]:
    """The capabilities ``interface`` reports, by value, in the enum's order.

    The module answers for itself (:meth:`InputModule.capabilities`), so a
    capability can follow the module's own settings. Values outside
    :class:`ModuleCapability` are dropped. Anything that is not an input
    module at all — a source that browses but streams nothing — has none,
    and a module whose report raises is listed with none rather than
    failing the whole listing.
    """
    module = getattr(interface, "wrapped", interface)
    if not isinstance(module, InputModule):
        return []
    try:
        reported = set(module.capabilities())
    except Exception as e:  # noqa: BLE001 — one plugin must not sink the listing
        logger.warning("%s.capabilities failed: %r", type(module).__name__, e)
        return []
    return [c.value for c in ModuleCapability if c in reported]
