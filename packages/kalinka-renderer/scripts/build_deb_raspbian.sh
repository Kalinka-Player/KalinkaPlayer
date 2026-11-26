#!/usr/bin/env bash
# Build the renderer deb for 32-bit Raspberry Pi OS (Raspbian trixie, ARMv6 with VFP) in a
# Raspbian chroot: Debian's own armhf targets ARMv7, which a Pi 1 or the first Zero cannot run.
# Needs root, debootstrap, gpg, gpgv and curl, and an ARM CPU or qemu-arm in binfmt_misc
# (fix-binary). The chroot is kept at RASPBIAN_ROOT and has no git, so set RENDERER_VERSION.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$(cd "$PKG_DIR/../.." && pwd)"
ROOT="${RASPBIAN_ROOT:-/var/tmp/kalinka-raspbian-trixie-armhf}"
MIRROR="${RASPBIAN_MIRROR:-http://raspbian.raspberrypi.com/raspbian}"
# Raspbian's archive signing key, "Mike Thompson (Raspberry Pi Debian armhf ARMv6+VFP)".
KEY_FINGERPRINT=A0DA38D0D76E8B5D638872819165938D90FDDD2E
# The same build dependencies as the other debs in renderer-release.yml.
BUILD_DEPS=(
  build-essential ca-certificates dpkg-dev cmake pkg-config libboost-dev
  libcurlpp-dev libssl-dev libcurl4-openssl-dev libflac++-dev libvorbis-dev
  libxml2-dev libfaad-dev libsoxr-dev libasound2-dev libspdlog-dev libfmt-dev
  libhwy-dev protobuf-compiler libprotobuf-dev
)

die() { echo "error: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root: debootstrap and chroot need it"
[ -n "${RENDERER_VERSION:-}" ] || die "set RENDERER_VERSION"

# debootstrap removes its own directory once it is done.
[ ! -d "$ROOT/debootstrap" ] \
  || die "an earlier debootstrap into $ROOT did not finish; remove it and run again"
if [ ! -x "$ROOT/usr/bin/apt-get" ]; then
  for tool in debootstrap gpg gpgv curl; do
    command -v "$tool" >/dev/null || die "$tool is needed to make the chroot"
  done
  keys="$(mktemp -d)"
  trap 'rm -rf "$keys"' EXIT
  curl -fsSL https://archive.raspbian.org/raspbian.public.key -o "$keys/raspbian.asc"
  fingerprint="$(gpg --homedir "$keys" --show-keys --with-colons "$keys/raspbian.asc" \
    | awk -F: '$1 == "fpr" { print $10; exit }')"
  [ "$fingerprint" = "$KEY_FINGERPRINT" ] \
    || die "the Raspbian archive key is $fingerprint, not $KEY_FINGERPRINT"
  gpg --dearmor < "$keys/raspbian.asc" > "$keys/raspbian.gpg"
  # The sid script, not trixie: it is what trixie names, and older debootstraps lack the name.
  debootstrap --arch=armhf --variant=minbase --keyring="$keys/raspbian.gpg" \
    --include=ca-certificates,raspbian-archive-keyring \
    trixie "$ROOT" "$MIRROR" /usr/share/debootstrap/scripts/sid
  rm -rf "$keys"
  trap - EXIT
fi
# Every run, so a kept chroot also gets a dependency added since it was made.
chroot "$ROOT" apt-get update -qq
DEBIAN_FRONTEND=noninteractive chroot "$ROOT" \
  apt-get install -y -qq --no-install-recommends "${BUILD_DEPS[@]}"

# Set before mounting, so a mount that fails does not leave the checkout mounted in the chroot.
unmount() {
  local dir
  for dir in "$ROOT/proc" "$ROOT/src"; do
    ! mountpoint -q "$dir" || umount "$dir"
  done
}
trap unmount EXIT
# The whole repository: the version scripts reach outside the package.
mkdir -p "$ROOT/src"
mount --bind "$REPO_DIR" "$ROOT/src"
mount -t proc proc "$ROOT/proc"
# Its own build tree, in the chroot: the checkout's build-release belongs to the host's builds.
chroot "$ROOT" env RENDERER_VERSION="$RENDERER_VERSION" BUILD_DIR=/var/tmp/kalinka-renderer-build \
  bash -c 'cd /src/packages/kalinka-renderer && scripts/build_deb.sh'
[ -z "${SUDO_UID:-}" ] \
  || chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$PKG_DIR"/kalinka-renderer-*.raspbian-*.armhf.deb
