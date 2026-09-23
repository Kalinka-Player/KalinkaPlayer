#!/bin/bash
# Build the kalinka-server .deb. The server is pure Python, so the package is
# Architecture: all — one artifact installs on any distro and arch, exactly
# like the plugin/SDK _all debs.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building wheel..."

if ! command -v python3 > /dev/null; then
    echo "Error: python3 not found."
    exit 1
fi

python3 -m pip install --upgrade build || true
rm -rf dist/*.whl

python3 -m build --wheel

WHEEL_PATH=$(ls dist/*.whl 2>/dev/null | sort -V | tail -1 || true)

if [ -z "$WHEEL_PATH" ] || [ ! -f "$WHEEL_PATH" ]; then
    echo "Error: No wheel could be built."
    exit 1
fi

echo "Getting version directly from source..."

WHEEL_VERSION=$(grep "__version__ = version =" "$SCRIPT_DIR/../src/kalinka_server/_version.py" | sed -n "s/.*['\"]\\([^'\"]*\\)['\"].*/\\1/p")

if [ -z "$WHEEL_VERSION" ]; then
    echo "Error: Could not determine package version."
    exit 1
fi

WHEEL_BASENAME=$(basename "$WHEEL_PATH")
TARGET_DIR="kalinka-server-$WHEEL_VERSION"
TARGET_FILE="kalinka-server_${WHEEL_VERSION}_all.deb"

echo "Using wheel: $WHEEL_BASENAME"
echo "Building Debian package with version: $WHEEL_VERSION"
echo "Target file: $TARGET_FILE"

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp -r DEBIAN "$TARGET_DIR"

sed "s/@VERSION@/$WHEEL_VERSION/" DEBIAN/control.in > "$TARGET_DIR/DEBIAN/control"
rm "$TARGET_DIR/DEBIAN/control.in"

mkdir -p "$TARGET_DIR/opt/kalinka/wheels"
mkdir -p "$TARGET_DIR/etc/systemd/system/"
mkdir -p "$TARGET_DIR/usr/lib/tmpfiles.d/"

cp "$WHEEL_PATH" "$TARGET_DIR/opt/kalinka/wheels/"
cp scripts/bootstrap.sh "$TARGET_DIR/opt/kalinka/bootstrap.sh"
chmod 755 "$TARGET_DIR/opt/kalinka/bootstrap.sh"
cp scripts/kalinka.service "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-restart.path "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-restart.service "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-upgrade.path "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-upgrade.service "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-journal-reader.socket "$TARGET_DIR/etc/systemd/system/"
cp scripts/kalinka-journal-reader@.service "$TARGET_DIR/etc/systemd/system/"
# In-place upgrade support: kalinka-upgrade.service runs this wrapper, which
# fetches the current published installer from kalinkaplayer.com at upgrade
# time (never a stale deb-shipped copy).
cp scripts/upgrade.sh "$TARGET_DIR/opt/kalinka/upgrade.sh"
chmod 755 "$TARGET_DIR/opt/kalinka/upgrade.sh"
cp scripts/kalinka.tmpfiles.conf "$TARGET_DIR/usr/lib/tmpfiles.d/kalinka.conf"
cp ../../README.md "$TARGET_DIR/opt/kalinka/"
cp LICENSE "$TARGET_DIR/opt/kalinka/"

dpkg-deb --root-owner-group --build "$TARGET_DIR"
mv "$TARGET_DIR.deb" "$TARGET_FILE"
rm -rf "$TARGET_DIR"

echo "Successfully built: $TARGET_FILE"
