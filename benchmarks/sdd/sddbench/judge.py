"""The independent judge: a general sentence-embedding model, not CLAP's.

Grading a CLAP ranking with CLAP's own text tower would ask the system under
test to mark its own paper. This judge is all-mpnet-base-v2 — a
general-purpose sentence encoder trained on text pairs, with no audio in its
history — run under onnxruntime from a pinned repository revision, so a rerun
grades against the same model and a swapped checkpoint is a changed
fingerprint rather than a silent shift.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

MODEL_REPO = "sentence-transformers/all-mpnet-base-v2"
#: Pinned commit of the model repository, not a moving branch.
MODEL_REVISION = "e8c3b32edf5434bc2275fc9bab85f82640a19130"
MODEL_FILES = ("onnx/model.onnx", "tokenizer.json", "sentence_bert_config.json")
MAX_TOKENS = 384
BATCH = 32


@dataclass(frozen=True)
class JudgeInfo:
    repo: str
    revision: str
    max_tokens: int
    pooling: str = "mean over the attention mask, then L2 normalise"
    runtime: str = "onnxruntime"


def provision(model_dir: Path) -> dict[str, Path]:
    """Fetch the pinned model files, cached under ``model_dir``."""
    from huggingface_hub import hf_hub_download

    paths = {}
    for name in MODEL_FILES:
        paths[name] = Path(
            hf_hub_download(
                MODEL_REPO, name, revision=MODEL_REVISION, local_dir=model_dir
            )
        )
    return paths


class Judge:
    """Encodes text to unit vectors. One session, reused for the whole run."""

    def __init__(self, model_dir: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        paths = provision(model_dir)
        self._np = np
        self._tokenizer = Tokenizer.from_file(str(paths["tokenizer.json"]))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding()
        self._session = ort.InferenceSession(
            str(paths["onnx/model.onnx"]),
            providers=["CPUExecutionProvider"],
        )
        self._inputs = {i.name for i in self._session.get_inputs()}

    def encode(self, texts: Sequence[str]):
        np = self._np
        vectors = []
        for start in range(0, len(texts), BATCH):
            batch = list(texts[start : start + BATCH])
            encoded = self._tokenizer.encode_batch(batch)
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, {k: v for k, v in feed.items()
                                              if k in self._inputs})[0]
            expanded = mask[..., None].astype(np.float32)
            pooled = (hidden * expanded).sum(axis=1) / np.clip(
                expanded.sum(axis=1), 1e-9, None
            )
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            vectors.append(pooled / np.clip(norms, 1e-9, None))
        return np.concatenate(vectors, axis=0) if vectors else np.zeros((0, 768))


def similarity_matrix(
    judge: Judge, caption_ids: Sequence[str], texts: Sequence[str]
):
    """Cosine similarity of every caption against every caption."""
    vectors = judge.encode(texts)
    return vectors @ vectors.T


def write_matrix(matrix, caption_ids: Sequence[str], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["caption_id", *caption_ids])
        for row_id, row in zip(caption_ids, matrix):
            writer.writerow([row_id, *(f"{value:.4f}" for value in row)])


def read_matrix(path: Path) -> dict[str, dict[str, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)[1:]
        return {
            row[0]: {other: float(value) for other, value in zip(header, row[1:])}
            for row in reader
        }


def fingerprint(model_dir: Path) -> dict:
    info = JudgeInfo(MODEL_REPO, MODEL_REVISION, MAX_TOKENS)
    return {
        **info.__dict__,
        "files": {
            name: _sha256(model_dir / name)
            for name in MODEL_FILES
            if (model_dir / name).exists()
        },
    }


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
