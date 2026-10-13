"""Shared text-embedding contract.

The server owns a single text-embedding model (one copy in memory — plugins
must not load their own) and hands it to every plugin through
``PluginContextBase.embedder``. The pair ``(model_id, model_version)``
identifies the embedding space. Plugins that persist vectors derived from it
(or ship indexes built offline) must validate both before use and treat a
mismatch as "recompute or disable" — vectors from different models, or from
different asset versions of the same model, are not comparable.

The SDK carries only this protocol; the implementation lives in the server.
Its runtime, ``onnxruntime`` and ``tokenizers``, is not installed with the
server: a plugin that embeds declares both in ``OPTIONAL_PACKAGES`` and
reports them in ``missing_packages`` while they cannot be imported, so the
user can install them. Added in SDK 1.2 — a plugin that requires
``context.embedder`` must pin ``kalinka-plugin-sdk>=1.2``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Protocol, Sequence

if TYPE_CHECKING:
    import numpy as np


class TextEmbedder(Protocol):
    """A shared sentence-embedding model.

    Implementations lazy-load: constructing one is free, the model is
    provisioned (downloaded if missing) and loaded on first use. All methods
    are safe to call concurrently; inference is serialized internally.
    """

    @property
    def model_id(self) -> str:
        """Stable identifier of the underlying model (e.g.
        ``"all-MiniLM-L6-v2"``). Changes when the server switches to a
        different model family."""
        ...

    @property
    def model_version(self) -> int:
        """Version of the model assets. Bumped whenever the vectors the
        model produces change — a re-exported ONNX graph, a different
        tokenizer or pooling — even if ``model_id`` stays the same.
        Plugins pin the exact ``(model_id, model_version)`` their
        precomputed vectors were built with."""
        ...

    @property
    def dim(self) -> int:
        """Dimensionality of the vectors returned by :meth:`embed`."""
        ...

    async def available(self) -> bool:
        """True once the runtime is installed and the model assets are
        present (downloading them if needed). False means embedding is not
        possible on this install; callers should degrade their feature, not
        raise."""
        ...

    async def embed(self, texts: Sequence[str]) -> "List[np.ndarray]":
        """Encode ``texts`` into one float32 L2-normalized ``(dim,)`` vector
        each. Raises ``RuntimeError`` when the embedder is unavailable —
        check :meth:`available` first."""
        ...
