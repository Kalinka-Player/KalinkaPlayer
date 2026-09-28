#!/usr/bin/env bash
#
# Render the plugin template once per plugin type, install what it generates
# next to the SDK in this tree, and run the generated plugin's own tests.
#
# Each plugin gets a venv of its own, apart from the one cookiecutter runs in,
# so a dependency the plugin forgot to declare cannot hide behind one of
# cookiecutter's. Needs python3 with venv, and the network: cookiecutter and
# the plugins' build requirements come from PyPI.
#
# Usage: bash scripts/test-plugin-template.sh   (PYTHON picks the interpreter)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
template="$repo/template/cookiecutter-kalinka-plugin"
sdk="$repo/packages/kalinka-plugin-sdk"
python="${PYTHON:-python3}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"$python" -m venv "$work/tools"
"$work/tools/bin/pip" wheel --quiet --disable-pip-version-check --no-deps \
  --wheel-dir "$work/wheels" "$sdk"
sdk=$(echo "$work"/wheels/kalinka_plugin_sdk-*.whl)
"$work/tools/bin/pip" install --quiet --disable-pip-version-check cookiecutter pytest "$sdk"
"$work/tools/bin/python" -m pytest -p no:cacheprovider "$template/tests"

for plugin_type in input_module device; do
  echo "Rendering the template as a $plugin_type plugin"
  "$work/tools/bin/cookiecutter" --no-input --default-config \
    --output-dir "$work/$plugin_type" "$template" plugin_type="$plugin_type"
  plugin="$work/$plugin_type/kalinka-plugin-myawesome"
  venv="$work/$plugin_type/venv"

  "$python" -m venv "$venv"
  "$venv/bin/pip" install --quiet --disable-pip-version-check pytest "$sdk" "$plugin"
  (cd "$plugin" && "$venv/bin/python" -m pytest tests/)
done
