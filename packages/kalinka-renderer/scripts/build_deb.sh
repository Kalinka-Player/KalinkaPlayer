#!/bin/bash
# Build the platform-specific kalinka-renderer .deb for the current arch.
# Release build, symbols stripped. Run from packages/kalinka-renderer on a
# Debian/Ubuntu system (or container) matching the deployment target — the
# binary links this distro's library sonames and dpkg-shlibdeps derives the
# Depends from them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PKG_DIR"

ARCH=$(dpkg --print-architecture)
# shellcheck source=version.sh
. "$SCRIPT_DIR/version.sh"
VERSION=$(renderer_version)
if [ -z "$VERSION" ]; then
    echo "Error: could not determine the renderer version." >&2
    exit 1
fi
echo "Building version: $VERSION"

BUILD_DIR=build-release
cmake -S . -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release \
    -DKALINKA_VERSION="$VERSION"
cmake --build "$BUILD_DIR" -j "$(nproc)" --target kalinka-renderer

# Subshell: os-release defines its own VERSION and would clobber ours.
PLATFORM=$(
    # shellcheck disable=SC1091
    [ -r /etc/os-release ] && . /etc/os-release
    echo "${ID:-unknown}-${VERSION_ID:-${VERSION_CODENAME:-unknown}}"
)

TARGET_DIR="kalinka-renderer-$VERSION"
TARGET_FILE="$TARGET_DIR.$PLATFORM.$ARCH.deb"
rm -rf "$TARGET_DIR"

mkdir -p "$TARGET_DIR/usr/bin" "$TARGET_DIR/usr/lib/systemd/system" \
    "$TARGET_DIR/opt/kalinka" "$TARGET_DIR/DEBIAN"
install -m 755 "$BUILD_DIR/kalinka-renderer" "$TARGET_DIR/usr/bin/kalinka-renderer"
strip --strip-unneeded "$TARGET_DIR/usr/bin/kalinka-renderer"
install -m 644 scripts/kalinka-renderer.service "$TARGET_DIR/usr/lib/systemd/system/"
# The upgrade plane: the renderer asks by touching a file, root does the work.
install -m 644 scripts/kalinka-renderer-upgrade.path "$TARGET_DIR/usr/lib/systemd/system/"
install -m 644 scripts/kalinka-renderer-upgrade.service "$TARGET_DIR/usr/lib/systemd/system/"
# Shipped inert: enabling it is how a box opts into upgrading without a Core.
install -m 644 scripts/kalinka-renderer-upgrade.timer "$TARGET_DIR/usr/lib/systemd/system/"
install -m 755 scripts/upgrade-renderer.sh "$TARGET_DIR/opt/kalinka/upgrade-renderer.sh"

# Depends from what the stripped binary actually links on this distro.
SHLIBDEPS=""
if command -v dpkg-shlibdeps > /dev/null 2>&1; then
    SHLIB_WORK=$(mktemp -d)
    mkdir -p "$SHLIB_WORK/debian"
    printf 'Source: kalinka-renderer\n\nPackage: kalinka-renderer\nArchitecture: any\nDepends: ${shlibs:Depends}\n' > "$SHLIB_WORK/debian/control"
    SHLIBDEPS=$( cd "$SHLIB_WORK" && dpkg-shlibdeps -O --ignore-missing-info "$PKG_DIR/$TARGET_DIR/usr/bin/kalinka-renderer" 2>/dev/null | sed -n 's/^shlibs:Depends=//p' )
    rm -rf "$SHLIB_WORK"
fi
if [ -z "$SHLIBDEPS" ]; then
    echo "dpkg-shlibdeps unavailable; using static library deps"
    SHLIBDEPS="libcurlpp0, libcurl4, libflac++10, libvorbisfile3, libasound2, libspdlog1.10, libfmt9, libprotobuf-lite32, libstdc++6, libc6, libgcc-s1"
fi
echo "Library deps: $SHLIBDEPS"

sed "s/@ARCH@/$ARCH/; s/@VERSION@/$VERSION/; s|@SHLIBDEPS@|$SHLIBDEPS|" \
    DEBIAN/control.in > "$TARGET_DIR/DEBIAN/control"
install -m 755 DEBIAN/postinst "$TARGET_DIR/DEBIAN/postinst"
install -m 755 DEBIAN/prerm "$TARGET_DIR/DEBIAN/prerm"

dpkg-deb --root-owner-group --build "$TARGET_DIR"
mv "$TARGET_DIR.deb" "$TARGET_FILE"
rm -rf "$TARGET_DIR"

echo "Successfully built: $TARGET_FILE"
ls -lh "$TARGET_FILE"
