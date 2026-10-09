"""Optional pip packages this plugin can install on demand.

Static allow-list. Pinned versions live here, not in install requests.
Importing this module must not pull heavy ML deps — it's loaded by the
deb build script to emit /opt/kalinka/allowed_packages/localfiles.json
and by the running server to register the catalog.

Run as a module to print the manifest JSON:

    python -m kalinka_plugin_localfiles.optional_packages
"""

from __future__ import annotations

import json
import sys

from kalinka_plugin_sdk import OptionalPackageSpec


PLUGIN_ID = "localfiles"
MANIFEST_SCHEMA_VERSION = 1


# Each plugin's internal sub-features are now the authority on "this
# functionality needs these packages" — see KalinkaPluginLocalFiles
# subfeature bookkeeping in module_setup.py.
OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec] = {
    # The server's own deps keep its specs, so an install never moves its copy.
    "numpy": OptionalPackageSpec(
        pip_spec="numpy>=1.24",
        description=(
            "Numerical core required by the searcher's mood ranking, "
            "CLAP audio embedding (embedder), and the enricher's generated "
            "album art."
        ),
    ),
    "onnxruntime": OptionalPackageSpec(
        pip_spec="onnxruntime>=1.17",
        description=(
            "ONNX inference runtime for the CLAP audio/text encoder used "
            "by AI search."
        ),
    ),
    "soundfile": OptionalPackageSpec(
        pip_spec="soundfile==0.13.1",
        description=(
            "Header-aware audio decoder used by the CLAP embedder. "
            "Reads only the 10 s fragments we need instead of the whole "
            "file (the librosa path it replaced OOM'd on long tracks)."
        ),
    ),
    "soxr": OptionalPackageSpec(
        pip_spec="soxr==1.0.0",
        description=(
            "Resampling kernel paired with soundfile in the CLAP "
            "embedder; only invoked when the source sample rate "
            "differs from 48 kHz."
        ),
    ),
    "tokenizers": OptionalPackageSpec(
        pip_spec="tokenizers>=0.15",
        description="HuggingFace tokenizers used by the CLAP text encoder.",
    ),
}


def manifest() -> dict:
    """Build the JSON-serialisable manifest payload."""
    return {
        "plugin": PLUGIN_ID,
        "schema": MANIFEST_SCHEMA_VERSION,
        "packages": {
            key: spec.model_dump() for key, spec in OPTIONAL_PACKAGES.items()
        },
    }


if __name__ == "__main__":
    json.dump(manifest(), sys.stdout, indent=2)
    sys.stdout.write("\n")
