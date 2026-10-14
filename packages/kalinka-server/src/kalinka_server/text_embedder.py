"""Shared MiniLM text embedder (all-MiniLM-L6-v2, ONNX, torch-free).

The server-owned implementation of ``kalinka_plugin_sdk.embedding.TextEmbedder``:
one model instance, handed to every plugin via ``PluginContextBase.embedder``
and usable by the server itself. Tokenize -> attention-masked mean-pool ->
L2-normalize, producing float32 L2-normalized 384-d vectors.

Lazy end to end: constructing the service is free; assets are provisioned
(downloaded if missing) and the ONNX session is loaded on first use, so an
install where nothing embeds never pays the resident cost. Moved here from
the Jamendo plugin (its mood index is built in this exact embedding space);
on first provision the model files are migrated from the plugin's legacy
directory to avoid a re-download.

The runtime (onnxruntime, tokenizers) is not a server dependency: a plugin
that embeds offers it as optional packages, asking ``missing_runtime()``
which ones this box lacks. Without it the embedder reports itself
unavailable before downloading anything.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import urllib.request
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__.split(".")[-1])

MODEL_ID = "all-MiniLM-L6-v2"
# Bump whenever the produced vectors change (re-exported ONNX, different
# tokenizer/pooling) — consumers pin the exact (MODEL_ID, MODEL_VERSION)
# their precomputed vectors were built with, so a silent asset swap would
# otherwise return near-random neighbours. Keep in sync with the assets at
# EmbeddingConfig.model_url and with kalinka-training jamendomaxcaps_embed.py.
MODEL_VERSION = 1
DIM = 384
MAX_TOKENS = 256
_FILES = ("model.onnx", "tokenizer.json")
_RUNTIME_PACKAGES = ("onnxruntime", "tokenizers")


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def download_file(url: str, dest: str) -> bool:
    """Download url -> dest atomically (via a .part temp). True on success."""
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        logger.info("downloading %s", url)
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        logger.warning("download failed (%s): %s", url, e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def _migrate_legacy_files(model_dir: str, legacy_dirs: Sequence[str]) -> None:
    """Move model files left behind by older releases (which stored them in a
    plugin-private directory) into ``model_dir`` instead of re-downloading."""
    for legacy in legacy_dirs:
        legacy = os.path.expanduser(legacy)
        for name in _FILES:
            src = os.path.join(legacy, name)
            dest = os.path.join(model_dir, name)
            if os.path.exists(dest) or not os.path.exists(src):
                continue
            try:
                os.makedirs(model_dir, exist_ok=True)
                os.replace(src, dest)
                logger.info("migrated %s from %s", name, legacy)
            except OSError as e:
                logger.warning("could not migrate %s from %s: %s", name, legacy, e)
        try:
            os.rmdir(legacy)  # only succeeds once empty
        except OSError:
            pass


def ensure_model(model_dir: str, base_url: Optional[str]) -> bool:
    """Ensure model.onnx + tokenizer.json are in model_dir, downloading any
    missing file from ``{base_url}/{file}``. True if both are present."""
    model_dir = os.path.expanduser(model_dir)
    for name in _FILES:
        dest = os.path.join(model_dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            continue
        if not base_url or not download_file(f"{base_url.rstrip('/')}/{name}", dest):
            return False
    return True


class MiniLmOnnx:
    def __init__(self, model_dir: str):
        self._model_dir = os.path.expanduser(model_dir)
        self._session = None
        self._tokenizer = None
        self._input_names: set = set()

    @property
    def is_loaded(self) -> bool:
        return self._session is not None

    def load(self) -> None:
        if self.is_loaded:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = os.path.join(self._model_dir, "model.onnx")
        tok_path = os.path.join(self._model_dir, "tokenizer.json")
        if not os.path.exists(model_path) or not os.path.exists(tok_path):
            raise FileNotFoundError(
                f"MiniLM model/tokenizer missing in {self._model_dir}"
            )
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        self._session = ort.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(tok_path)
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        logger.info("MiniLM ONNX encoder loaded from %s", self._model_dir)

    def unload(self) -> None:
        self._session = None
        self._tokenizer = None
        self._input_names = set()

    def encode_one(self, text: str):
        """Encode a single query string -> (384,) float32 L2-normalized ndarray."""
        import numpy as np

        if not self.is_loaded:
            self.load()
        enc = self._tokenizer.encode(text or "")
        ids = np.asarray([enc.ids], np.int64)
        mask = np.asarray([enc.attention_mask], np.int64)
        feeds = {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": np.zeros_like(ids),
        }
        feeds = {k: v for k, v in feeds.items() if k in self._input_names}
        hidden = self._session.run(None, feeds)[0]  # (1, seq, 384)
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        vec = pooled[0]
        norm = float(np.linalg.norm(vec))
        return (vec / norm if norm > 0 else vec).astype(np.float32)


class SharedTextEmbedder:
    """Implements the SDK ``TextEmbedder`` protocol over :class:`MiniLmOnnx`.

    One asyncio.Lock serializes provisioning and inference — one model, low
    QPS across all consumers. Blocking work (downloads, ONNX) always runs in
    an executor.
    """

    def __init__(self, model_dir: str, model_url: Optional[str],
                 legacy_dirs: Sequence[str] = ()):
        self._model_dir = os.path.expanduser(model_dir)
        self._model_url = model_url
        self._legacy_dirs = tuple(legacy_dirs)
        self._encoder = MiniLmOnnx(self._model_dir)
        self._lock = asyncio.Lock()
        self._available: Optional[bool] = None
        # Probed once: installing the runtime always restarts the server.
        self._missing_runtime: Optional[List[str]] = None

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> int:
        return MODEL_VERSION

    @property
    def dim(self) -> int:
        return DIM

    def missing_runtime(self) -> List[str]:
        if self._missing_runtime is None:
            self._missing_runtime = [
                name for name in _RUNTIME_PACKAGES if not _importable(name)
            ]
        return list(self._missing_runtime)

    async def available(self) -> bool:
        if self._available is None:
            async with self._lock:  # one provisioning; others await it
                if self._available is None:
                    loop = asyncio.get_running_loop()
                    self._available = await loop.run_in_executor(
                        None, self._provision)
        return self._available

    def _provision(self) -> bool:
        missing = self.missing_runtime()
        if missing:
            logger.warning("%s not installed; text embedding off",
                           ", ".join(missing))
            return False
        _migrate_legacy_files(self._model_dir, self._legacy_dirs)
        if not ensure_model(self._model_dir, self._model_url):
            logger.warning("MiniLM model missing (%s); text embedding off",
                           self._model_dir)
            return False
        return True

    async def embed(self, texts: Sequence[str]) -> List:
        if not await self.available():
            raise RuntimeError("text embedder unavailable")
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: [self._encoder.encode_one(t) for t in texts])
