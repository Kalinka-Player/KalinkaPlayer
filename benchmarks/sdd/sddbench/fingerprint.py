"""What this run was: the code, the checkpoints, the settings, the judge.

A number without this is not reproducible, and two numbers without it are not
comparable. Everything here is read from the machine that ran, never typed in.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import judge
from .paths import REPO_ROOT, Layout

_PACKAGES = (
    "kalinka-server",
    "kalinka-plugin-localfiles",
    "kalinka-plugin-sdk",
    "onnxruntime",
    "numpy",
    "soundfile",
    "soxr",
    "sqlite-vec",
    "tokenizers",
    "mutagen",
    "httpx",
)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _versions() -> dict[str, str]:
    found = {}
    for name in _PACKAGES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = "absent"
    return found


def _models(layout: Layout) -> dict:
    from kalinka_plugin_localfiles.embedder import clap_onnx
    from kalinka_plugin_localfiles.embedding_utils import (
        CLAP_EMBED_FORMAT_VERSION,
        CLAP_INT8_CAP,
        CLAP_MODEL_VERSION,
        VA_HEAD_VERSION,
    )

    files = {}
    for name, filename in clap_onnx._MODEL_FILENAMES.items():
        path = layout.models / filename
        if path.exists():
            files[filename] = {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "url": clap_onnx._MODEL_URLS.get(name, ""),
            }
    return {
        "release": clap_onnx._RELEASE_BASE,
        "clap_model_version": CLAP_MODEL_VERSION,
        "clap_embed_format_version": CLAP_EMBED_FORMAT_VERSION,
        "va_head_version": VA_HEAD_VERSION,
        "int8_cap": CLAP_INT8_CAP,
        "sample_rate": clap_onnx._SAMPLE_RATE,
        "fragment_seconds": clap_onnx._FRAGMENT_SECONDS,
        "text_token_limit": clap_onnx._TOKEN_MAX_LEN,
        "files": files,
    }


def touches() -> list[dict]:
    """The instrumentation this run carried, named by the shim itself."""
    timing_dir = str(Path(__file__).resolve().parent / "timing")
    if timing_dir not in sys.path:
        sys.path.insert(0, timing_dir)
    import bench_timing

    return [
        {"symbol": symbol, "measures": what, "kind": kind}
        for symbol, what, kind in bench_timing.TOUCHES
    ]


def collect(layout: Layout, config: dict, dataset_stats: dict) -> dict:
    data = {
        "code": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
            "describe": _git("describe", "--tags", "--always", "--dirty"),
        },
        "host": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": len(__import__("os").sched_getaffinity(0)),
        },
        "packages": _versions(),
        "models": _models(layout),
        "server_config": config,
        "judge": judge.fingerprint(layout.judge_model),
        "dataset": {
            "zenodo_record": "10072001",
            "doi": "10.5281/zenodo.10072001",
            **{k: v for k, v in dataset_stats.items() if k != "leakage_check"},
        },
        "instrumentation": touches(),
    }
    layout.fingerprint.write_text(json.dumps(data, indent=2))
    return data
