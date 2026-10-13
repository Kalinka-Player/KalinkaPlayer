"""Optional pip packages this plugin can install on demand.

The runtime of the server's shared text embedder, which mood search needs
and the server does not install. Importing this module must not pull the
packages it lists: the deb and RPM builds run it as a script to emit
/opt/kalinka/allowed_packages/jamendo.json, and the running server reads the
allow-list from it.

Run as a module to print the manifest JSON:

    python -m kalinka_plugin_jamendo.optional_packages
"""

from __future__ import annotations

import json
import sys

from kalinka_plugin_sdk import OptionalPackageSpec


PLUGIN_ID = "jamendo"
MANIFEST_SCHEMA_VERSION = 1


# localfiles offers the same keys, and a shared key must keep one spec.
OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec] = {
    "onnxruntime": OptionalPackageSpec(
        pip_spec="onnxruntime>=1.17",
        description=(
            "ONNX inference runtime for the text embedder that turns a mood "
            "search query into the vector the Jamendo index is matched on."
        ),
    ),
    "tokenizers": OptionalPackageSpec(
        pip_spec="tokenizers>=0.15",
        description=(
            "HuggingFace tokenizers used by the text embedder behind mood "
            "search."
        ),
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
