#!/bin/bash
# Builds a Raspberry Pi image with rpi-image-gen, inside a Debian container so
# any Linux host with podman and binfmt support for the target can run it.
#
# Usage: build.sh [config]     config relative to this directory,
#                              default config/kalinka-rpi5.yaml
# Env:
#   KALINKA_VERSION    Kalinka release to install (default: the latest)
#   SUPERVISOR_DEB     an arm64 kalinka-supervisor package (default: build one)
#   SUPERVISOR_VERSION the version of the one built (default: from the last tag)
#   RENDERER_DEB       for rpi1-renderer, an armhf renderer package to install
#                      instead of the latest release's
#   OUT_DIR            where the image lands (default: ../out/rpi-image-gen)
#   XZ_LEVEL           xz compression preset (default: -6)
#   TEST_DEBS_DIR      local arm64/all packages for a Bluetooth test image
#   GITHUB_TOKEN       optional, raises the GitHub API rate limit while the
#                      install finds the release
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
OUT_DIR="${OUT_DIR:-$IMAGE_DIR/out/rpi-image-gen}"
SCRIPT_DIR="$IMAGE_DIR"

# shellcheck source=../lib/common.sh
. "$IMAGE_DIR/lib/common.sh"
# shellcheck source=../lib/supervisor.sh
. "$IMAGE_DIR/lib/supervisor.sh"
# shellcheck source=../lib/seal.sh
. "$IMAGE_DIR/lib/seal.sh"
# shellcheck source=../lib/test-packages.sh
. "$IMAGE_DIR/lib/test-packages.sh"

[ -f "$HERE/$CONFIG" ] || die "no config $CONFIG"
target="$(basename "$CONFIG" .yaml)"
target="${target#kalinka-}"

# The renderer image is for a Pi 1, whose ARMv6 core only Raspbian's armhf runs; the others are 64-bit.
RENDERER_ONLY=0 TARGET_ARCH=arm64 QEMU_ARCH=aarch64 NATIVE_MACHINE='aarch64'
[ "$target" != rpi1-renderer ] \
  || RENDERER_ONLY=1 TARGET_ARCH=armhf QEMU_ARCH=arm NATIVE_MACHINE='armv[678]l'
[ -e "/proc/sys/fs/binfmt_misc/qemu-$QEMU_ARCH" ] || [[ "$(uname -m)" == $NATIVE_MACHINE ]] \
  || die "$TARGET_ARCH binaries need qemu-user-static registered with binfmt_misc"
if [ "$RENDERER_ONLY" = 1 ]; then
  [ -z "${TEST_DEBS_DIR:-}${SPIKE_SSH_PUBKEY:-}" ] \
    || die "TEST_DEBS_DIR and SPIKE_SSH_PUBKEY are for the full images"
  [ -z "${RENDERER_DEB:-}" ] || [ -r "$RENDERER_DEB" ] || die "cannot read $RENDERER_DEB"
else
  [ -z "${RENDERER_DEB:-}" ] || die "RENDERER_DEB is for the rpi1-renderer image"
fi

overrides=(IGconf_kalinka_contents=/work/contents)
if [ "$RENDERER_ONLY" = 0 ]; then
  overrides+=(IGconf_kalinka_supervisor_deb=/work/kalinka-supervisor.deb)
  [ -z "${KALINKA_VERSION:-}" ] || overrides+=("IGconf_kalinka_version=\"$KALINKA_VERSION\"")
fi
if [ -n "${SPIKE_SSH_PUBKEY:-}" ]; then
  [ -r "$SPIKE_SSH_PUBKEY" ] || die "cannot read $SPIKE_SSH_PUBKEY"
  overrides+=(IGconf_device_user1=kalinka-dev IGconf_device_user1sudo=nopasswd
              "IGconf_ssh_pubkey_user1=\"$(cat "$SPIKE_SSH_PUBKEY")\""
              IGconf_layer_access=kalinka-spike-access)
  target+=-spike
fi

mkdir -p "$IMAGE_DIR/cache" "$OUT_DIR"
[ -d "$RIG_DIR" ] || git clone -q --depth 1 --branch "$RIG_REF" \
  https://github.com/raspberrypi/rpi-image-gen.git "$RIG_DIR"

# On disk, not in /tmp: the root alone is gigabytes.
WORK="$(mktemp -d "$IMAGE_DIR/cache/rpi-image-gen-work.XXXXXXXX")"
trap 'podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"' EXIT
mkdir "$WORK/build"
if [ -n "${TEST_DEBS_DIR:-}" ]; then
  stage_test_packages "$TEST_DEBS_DIR" "$WORK/test-debs" "$TARGET_ARCH" "${SUPERVISOR_DEB:-}"
  overrides+=(IGconf_kalinka_test_debs=/work/test-debs)
  target+=-test
  supervisor_file="$(awk -F '\t' '$1 == "kalinka-supervisor" { print $4 }' "$WORK/test-debs/packages.tsv")"
  if [ -z "${SUPERVISOR_DEB:-}" ] && [ -n "$supervisor_file" ]; then
    SUPERVISOR_DEB="$WORK/test-debs/$supervisor_file"
  fi
fi
if [ -n "${RENDERER_DEB:-}" ]; then
  cp "$RENDERER_DEB" "$WORK/"
  overrides+=("IGconf_kalinka_renderer_deb=/work/${RENDERER_DEB##*/}")
fi
# rpi-image-gen prints every setting it is given, so the token goes over as a file.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  ( umask 077 && printf '%s' "$GITHUB_TOKEN" > "$WORK/github-token" )
  overrides+=(IGconf_kalinka_github_token_file=/work/github-token)
fi

[ "$RENDERER_ONLY" = 1 ] || build_supervisor

if ! podman image exists "$BUILDER"; then
  # Shared CI runners keep hitting Docker Hub's anonymous pull limit, so prefer its ECR mirror.
  base="$(bash "$IMAGE_DIR/tests/pull-base.sh" podman public.ecr.aws/docker/library/debian:trixie)"
  podman build --security-opt label=disable --build-arg BASE="$base" -t "$BUILDER" -f - "$RIG_DIR" <<'CONTAINERFILE'
ARG BASE
FROM $BASE
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
image="$(find "$WORK/build" -mindepth 2 -maxdepth 2 -path "$WORK/build/image-*/*.img" -type f | head -n1)"
[ -n "$image" ] || die 'rpi-image-gen left no image'
if [ "$RENDERER_ONLY" = 1 ]; then
  read -r renderer < "$WORK/contents"
  name="$(image_name "$renderer" "$target" "$TARGET_ARCH")"
  write_manifest "$OUT_DIR/$name.manifest.json" "$target" "$TARGET_ARCH" 0 "" "" "$renderer" \
    "rpi_image_gen=$RIG_REF"
else
  read -r core supervisor renderer display < "$WORK/contents"
  name="$(image_name "$core" "$target" "$TARGET_ARCH")"
  write_manifest "$OUT_DIR/$name.manifest.json" "$target" "$TARGET_ARCH" "$display" "$core" "$supervisor" "$renderer" \
    "rpi_image_gen=$RIG_REF"
fi
if [ -n "${TEST_DEBS_DIR:-}" ]; then
  cp "$WORK/test-debs/packages.tsv" "$OUT_DIR/$name.packages.tsv"
fi
compress_image "$image" "$name"
ls -lh "$OUT_DIR/$name.xz"
