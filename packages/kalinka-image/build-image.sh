#!/usr/bin/env bash
#
# Build a bootable Kalinka Player appliance image: an operating system with the
# server, every first-party plugin, the browser player and the renderer already
# installed and enabled, ready to play as soon as it is powered on.
#
# Usage:
#   sudo ./build-image.sh <target> [kalinka-version]
#
# A target, targets/<name>.sh, names the hardware. It sets TARGET_ARCH (the
# dpkg architecture) and TARGET_BASE, the system Kalinka is installed on; the
# base, lib/base-<name>.sh, says what else a target must provide. Every base
# implements the same steps:
#
#   BASE_HOST_TOOLS          host commands the base needs
#   base_check_host          refuse a host that cannot build it
#   base_create_image        $IMAGE attached, root at $ROOTFS, boot mounted
#   base_configure_system    the files a bare root filesystem is missing
#   base_install_packages    the system packages Kalinka runs on
#   base_finish              first-boot machinery and bootloader
#   base_seal                the identities the base regenerates on first boot
#
# Env:
#   IMAGE_SIZE          image size before first-boot growth (default: 4GiB)
#   OUT_DIR             where the .img.xz lands (default: ./out)
#   XZ_LEVEL            xz compression preset (default: -6)
#   GITHUB_TOKEN        optional, raises the GitHub API rate limit
#
# Building for another architecture needs qemu-user-static registered with
# binfmt_misc on the host; the build says so if it is missing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # lib/kalinka.sh reads it
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

IMAGE_SIZE="${IMAGE_SIZE:-4GiB}"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/out}"
XZ_LEVEL="${XZ_LEVEL:--6}"

for lib in common chroot-aids kalinka seal; do
  # shellcheck source=/dev/null
  . "$SCRIPT_DIR/lib/$lib.sh"
done

TARGET="${1:-}"
[ -n "$TARGET" ] || die "usage: build-image.sh <target> [kalinka-version]"
[ -r "$SCRIPT_DIR/targets/$TARGET.sh" ] || die "unknown target '$TARGET'"
KALINKA_VERSION="${2:-}"

# shellcheck source=/dev/null
. "$SCRIPT_DIR/targets/$TARGET.sh"
[ -r "$SCRIPT_DIR/lib/base-$TARGET_BASE.sh" ] \
  || die "target $TARGET names an unknown base '$TARGET_BASE'"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/lib/base-$TARGET_BASE.sh"

require_root
require_host_tools sfdisk losetup blkid xz "${BASE_HOST_TOOLS[@]}"
base_check_host
require_foreign_arch_support "$TARGET_ARCH"

start_work
base_create_image
mount_chroot_filesystems
snapshot_packages
base_configure_system
use_build_dns
limit_journal
start_build_aids

in_chroot apt-get update --error-on=any
base_install_packages
install_kalinka "$KALINKA_VERSION"
verify_kalinka
base_finish

stop_build_aids
seal_rootfs
base_seal
zero_fill_free_space
publish_image
