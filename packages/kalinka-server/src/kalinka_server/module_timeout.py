"""Hard per-call timeout around input-module interfaces.

The SDK's ``InputModule`` latency contract says every call serves a
real-time request and must finish within the server's per-call budget.
This wrapper is the server-side enforcement: it doesn't matter what the
plugin does — a call that overruns is cancelled and surfaces as a
``TimeoutError``, which the call sites already treat like any other
plugin failure (a failed leg, a dropped shelf, a 500 on /browse).

Every ``InputModule`` protocol method is bound concretely in ``__init__``
so that ``isinstance(proxy, InputModule)`` still holds. This is load-bearing:
the server gates every module behind ``isinstance(..., InputModule)`` (browse
root, source resolution), and Python 3.12 changed
``runtime_checkable`` protocol checks to resolve members by *static* lookup,
which does NOT trigger ``__getattr__``. A pure-``__getattr__`` proxy therefore
silently fails the check on 3.12+ (every module skipped, empty catalog), even
though ``hasattr`` still reports every method. Binding real delegates keeps the
proxy indistinguishable from the module on all Python versions.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging

from kalinka_plugin_sdk.inputmodule import InputModule

logger = logging.getLogger(__name__.split(".")[-1])

# Keep in sync with the latency contract stated on the SDK's InputModule
# docstring — plugins size their backend HTTP timeouts against this.
PLUGIN_CALL_TIMEOUT_S = 3.0

# The protocol members the proxy must expose concretely (see module docstring).
_PROTOCOL_METHODS = tuple(InputModule.__protocol_attrs__)


class TimeLimitedInputModule:
    """Delegating proxy that applies the per-call budget to every async
    method of ``inner`` — the protocol methods bound in ``__init__`` and any
    other coroutine attribute reached through ``__getattr__``. Sync
    attributes pass through unchanged."""

    def __init__(self, inner: InputModule, label: str,
                 timeout_s: float = PLUGIN_CALL_TIMEOUT_S):
        self._inner = inner
        self._label = label
        self._timeout_s = timeout_s
        # Bind protocol members on the instance (see module docstring).
        for name in _PROTOCOL_METHODS:
            attr = getattr(inner, name, None)
            if attr is None:
                continue
            bound = self._budgeted(attr, name) if inspect.iscoroutinefunction(attr) else attr
            setattr(self, name, bound)

    def _budgeted(self, attr, name):
        @functools.wraps(attr)
        async def timed(*args, **kwargs):
            try:
                return await asyncio.wait_for(attr(*args, **kwargs), self._timeout_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s.%s exceeded the %.0fs per-call budget",
                    self._label, name, self._timeout_s,
                )
                raise TimeoutError(
                    f"{self._label}.{name} exceeded the "
                    f"{self._timeout_s:.0f}s per-call budget"
                ) from None

        return timed

    @property
    def wrapped(self) -> InputModule:
        """The module underneath, for a caller that has to inspect its class
        rather than call it. The delegates bound in ``__init__`` stand in
        front of the wrapped class, so ``type(proxy)`` says nothing about
        what the module actually implements."""
        return self._inner

    def __getattr__(self, name):
        # Attributes beyond the InputModule protocol (e.g. get_indexer_status,
        # which the server calls via hasattr) aren't bound in __init__, so they
        # arrive here. Budget them too if they're coroutine functions — every
        # async call path stays under the limit, not just the protocol ones.
        attr = getattr(self._inner, name)
        if inspect.iscoroutinefunction(attr):
            return self._budgeted(attr, name)
        return attr
