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

from kalinka_plugin_sdk.inputmodule import InputModule

logger = logging.getLogger(__name__)

#: Natural-language search over the source's own audio —
#: :meth:`InputModule.ai_search`, behind ``GET /ai_search``.
AI_SEARCH = "ai_search"

_OPTIONAL_CALLS = (AI_SEARCH,)

#: The SDK hook a module answers to say an implemented call is switched on.
_SWITCHES = {AI_SEARCH: "ai_search_available"}


def capabilities_of(interface: Any) -> List[str]:
    """The optional calls ``interface`` implements, in a stable order.

    Derived rather than declared, so a plugin gains a capability by writing
    the method and nothing else: the SDK's protocol carries a default body
    for every optional call, and a module that leaves one alone is asking not
    to be asked. A module that writes the call behind a setting answers its
    switch (``ai_search_available``) with False while the setting is off.
    Anything that is not an input module at all — a source that browses but
    streams nothing — has none of them.
    """
    module = getattr(interface, "wrapped", interface)
    if not isinstance(module, InputModule):
        return []
    return [name for name in _OPTIONAL_CALLS if _implements(module, name)]


def _implements(module: InputModule, call: str) -> bool:
    if getattr(type(module), call, None) is getattr(InputModule, call, None):
        return False
    switch = _SWITCHES.get(call)
    if switch is None:
        return True
    try:
        return bool(getattr(module, switch)())
    except Exception as e:  # noqa: BLE001 — one plugin must not sink the listing
        logger.warning("%s.%s failed: %r", type(module).__name__, switch, e)
        return False
