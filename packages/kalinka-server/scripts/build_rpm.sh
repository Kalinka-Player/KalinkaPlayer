#!/bin/bash
# Build the noarch kalinka-server RPM: the whole app bundle (server + SDK +
# first-party plugin wheels + optional-package manifests) in one package.
# Needs `build`, `setuptools-scm` and `pydantic` importable by python3 (the
# manifest export imports pydantic + the SDK sources).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"
PACKAGES_DIR="$(dirname "$PKG_DIR")"
cd "$PKG_DIR"

STAGE="$PKG_DIR/build-rpm-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/wheels" "$STAGE/allowed_packages"

echo "Building wheels for the whole bundle..."
for pkg in "$PKG_DIR" "$PACKAGES_DIR"/kalinka-plugin-*; do
    name="$(basename "$pkg")"
    echo "  - $name"
    (cd "$pkg" && rm -rf dist && python3 -m build --wheel -q)
    cp "$pkg"/dist/*.whl "$STAGE/wheels/"
done

echo "Exporting optional-packages manifests..."
# The SDK's own optional_packages.py matches too; it prints nothing.
for module in "$PACKAGES_DIR"/kalinka-plugin-*/src/kalinka_plugin_*/optional_packages.py; do
    manifest="$(PYTHONPATH="$PACKAGES_DIR/kalinka-plugin-sdk/src" python3 "$module")"
    [ -n "$manifest" ] || continue
    plugin="$(basename "$(dirname "$module")")"
    printf '%s\n' "$manifest" > "$STAGE/allowed_packages/${plugin#kalinka_plugin_}.json"
done

VERSION=$(grep "__version__ = version =" "$PKG_DIR/src/kalinka_server/_version.py" | sed -n "s/.*['\"]\\([^'\"]*\\)['\"].*/\\1/p")
if [ -z "$VERSION" ]; then
    echo "Error: could not determine the server version." >&2
    exit 1
fi

TOPDIR="$PKG_DIR/build-rpm"
rm -rf "$TOPDIR"

rpmbuild -bb --build-in-place \
    --define "server_version $VERSION" \
    --define "_topdir $TOPDIR" \
    rpm/kalinka-server.spec

RPM_PATH=$(find "$TOPDIR/RPMS" -name "kalinka-server-*.noarch.rpm" | head -1)
if [ -z "$RPM_PATH" ]; then
    echo "Error: rpmbuild produced no package." >&2
    exit 1
fi
cp "$RPM_PATH" "$PKG_DIR/"
echo "Successfully built: $(basename "$RPM_PATH")"
ls -lh "$PKG_DIR/$(basename "$RPM_PATH")"
