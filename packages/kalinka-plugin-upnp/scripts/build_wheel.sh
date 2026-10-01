#!/usr/bin/env bash
set -euo pipefail

PLUGIN_SLUG="kalinka-plugin-upnp"

echo "Building wheel for ${PLUGIN_SLUG} using setuptools_scm for version detection"

# Install required build tools
python3 -m pip install --upgrade build setuptools-scm

# Build the wheel (setuptools_scm will automatically handle version detection and _version.py creation)
python3 -m build --wheel
