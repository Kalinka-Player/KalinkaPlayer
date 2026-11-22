"""MiniLM sentence-embedding encoder (ONNX, torch-free).

`all-MiniLM-L6-v2` (BERT, 6 layers, 384-d) run via onnxruntime + the HF
`tokenizers` fast tokenizer. Mean-pools token embeddings (attention-mask
weighted) and L2-normalizes — exactly the sentence-transformers pooling for
this model. CPU-only, Pi-friendly.

This is the encoder used BOTH to build the JamendoMaxCaps text index
(`jamendomaxcaps_embed.py`) and, mirrored, to encode the query at runtime in
the Jamendo plugin. Query and corpus MUST use the same model + pooling, so keep
the two copies byte-identical (same `model.onnx`, same mean-pool+normalize).

Model files (download once, e.g. from Xenova/all-MiniLM-L6-v2):
  <model_dir>/model.onnx
  <model_dir>/tokenizer.json
"""
from __future__ import annotations

import os
from typing import List

import numpy as np

DIM = 384
MAX_TOKENS = 256  # sentence-transformers all-MiniLM-L6-v2 truncates here


class MiniLmOnnx:
    def __init__(self, model_dir: str):
        self._model_dir = os.path.expanduser(model_dir)
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()

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
                f"MiniLM model/tokenizer missing in {self._model_dir} "
                "(need model.onnx + tokenizer.json)"
            )
        opts = ort.SessionOptions()
        # Default 2 mirrors the Pi runtime; the offline build sets MINILM_THREADS
        # higher to use all cores (embedding the full corpus is CPU-bound).
        opts.intra_op_num_threads = int(os.environ.get("MINILM_THREADS", "2"))
        opts.inter_op_num_threads = 1
        # Keep RSS flat on the Pi (same rationale as clap_onnx).
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        self._session = ort.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(tok_path)
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)

    def unload(self) -> None:
        self._session = None
        self._tokenizer = None
        self._input_names = set()

    def encode(self, texts: List[str], batch_size: int = 64) -> np.ndarray:
        """Encode texts -> (N, 384) float32, L2-normalized."""
        if not self.is_loaded:
            self.load()
        out = np.empty((len(texts), DIM), dtype=np.float32)
        for start in range(0, len(texts), batch_size):
            chunk = texts[start:start + batch_size]
            out[start:start + len(chunk)] = self._encode_batch(chunk)
        return out

    def _encode_batch(self, texts: List[str]) -> np.ndarray:
        encs = [self._tokenizer.encode(t or "") for t in texts]
        maxlen = max((len(e.ids) for e in encs), default=1)
        ids = np.zeros((len(texts), maxlen), np.int64)
        mask = np.zeros((len(texts), maxlen), np.int64)
        for i, e in enumerate(encs):
            ids[i, : len(e.ids)] = e.ids
            mask[i, : len(e.attention_mask)] = e.attention_mask
        feeds = {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": np.zeros_like(ids),
        }
        feeds = {k: v for k, v in feeds.items() if k in self._input_names}
        hidden = self._session.run(None, feeds)[0]  # (N, seq, 384)
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)
