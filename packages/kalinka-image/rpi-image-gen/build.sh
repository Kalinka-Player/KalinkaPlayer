#!/bin/bash
# Builds a Raspberry Pi image with rpi-image-gen, inside a Debian container so
# any Linux host with podman and arm64 binfmt support can run it.
#
# Usage: build.sh [config]     config relative to this directory,
#                              default config/kalinka-rpi5.yaml
# Env:
#   KALINKA_VERSION    Kalinka release to install (default: the latest)
#   SUPERVISOR_DEB     an arm64 kalinka-supervisor package (default: build one)
#   SPIKE_SSH_PUBKEY   public key file: adds a kalinka-dev login with it and
#                      SSH on, for checking an image on a board. The image is
#                      named *-spike and is never to be published.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_DIR="$(dirname "$HERE")"
REPO_ROOT="$(cd "$IMAGE_DIR/../.." && pwd)"
CONFIG="${1:-config/kalinka-rpi5.yaml}"
RIG_REF=v2.9.0
RIG_DIR="$IMAGE_DIR/cache/rpi-image-gen-$RIG_REF"
BUILDER="localhost/kalinka-rpi-image-gen:$RIG_REF"
OUT_DIR="$IMAGE_DIR/out/rpi-image-gen"

die() { echo "build.sh: $*" >&2; exit 1; }

[ -f "$HERE/$CONFIG" ] || die "no config $CONFIG"
[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || [ "$(uname -m)" = aarch64 ] \
  || die 'arm64 binaries need qemu-user-static registered with binfmt_misc'

name="$(basename "$CONFIG" .yaml)"
overrides=(IGconf_kalinka_supervisor_deb=/work/kalinka-supervisor.deb)
[ -z "${KALINKA_VERSION:-}" ] || overrides+=("IGconf_kalinka_version=\"$KALINKA_VERSION\"")
if [ -n "${SPIKE_SSH_PUBKEY:-}" ]; then
  [ -r "$SPIKE_SSH_PUBKEY" ] || die "cannot read $SPIKE_SSH_PUBKEY"
  overrides+=(IGconf_device_user1=kalinka-dev IGconf_device_user1sudo=nopasswd
              "IGconf_ssh_pubkey_user1=\"$(cat "$SPIKE_SSH_PUBKEY")\""
              IGconf_layer_access=kalinka-spike-access)
  name+=-spike
fi

mkdir -p "$IMAGE_DIR/cache" "$OUT_DIR"
[ -d "$RIG_DIR" ] || git clone -q --depth 1 --branch "$RIG_REF" \
  https://github.com/raspberrypi/rpi-image-gen.git "$RIG_DIR"

# On disk, not in /tmp: the root alone is gigabytes.
WORK="$(mktemp -d "$IMAGE_DIR/cache/rpi-image-gen-work.XXXXXXXX")"
trap 'podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"' EXIT
mkdir "$WORK/build"

if [ -n "${SUPERVISOR_DEB:-}" ]; then
  cp "$SUPERVISOR_DEB" "$WORK/kalinka-supervisor.deb"
else
  GOARCH=arm64 OUT_DIR="$WORK" "$REPO_ROOT/packages/kalinka-supervisor/build-deb.sh"
  mv "$WORK"/kalinka-supervisor_*_arm64.deb "$WORK/kalinka-supervisor.deb"
fi

if ! podman image exists "$BUILDER"; then
  podman build --security-opt label=disable -t "$BUILDER" -f - "$RIG_DIR" <<'CONTAINERFILE'
FROM docker.io/library/debian:trixie
COPY . /rig
RUN apt-get update && /rig/install_deps.sh && rm -rf /var/lib/apt/lists/*
CONTAINERFILE
fi

podman run --rm --privileged --security-opt label=disable \
  -v "$REPO_ROOT:/src:ro" -v "$WORK:/work" \
  "$BUILDER" /rig/rpi-image-gen build \
    -S /src/packages/kalinka-image/rpi-image-gen \
    -c "/src/packages/kalinka-image/rpi-image-gen/$CONFIG" \
    -B /work/build \
    -- "${overrides[@]}"

# Only the image directories: the root left beside them is owned by the container's users.
image="$(find "$WORK"/build/image-* -maxdepth 1 -name '*.img' -type f | head -n1)"
[ -n "$image" ] || die 'rpi-image-gen left no image'
xz -T0 --stdout "$image" > "$OUT_DIR/$name.img.xz"
( cd "$OUT_DIR" && sha256sum "$name.img.xz" > "$name.img.xz.sha256" )
ls -lh "$OUT_DIR/$name.img.xz"
