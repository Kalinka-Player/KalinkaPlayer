#!/usr/bin/env bash
#
# The pieces of the build shared by every base that can be checked without
# root: the published file name, putting the image's own resolv.conf back, the
# overlay's modes, reading unit links, and telling what this build installed
# from what the base already had.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common chroot-aids kalinka seal; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOTFS="$WORK/rootfs"

echo "  -- image names"
assert_eq "a PC is named by its architecture alone" \
  "$(image_name 5.1.0 amd64 amd64)" "kalinka-5.1.0-amd64.img"
assert_eq "a board by its target and architecture" \
  "$(image_name 5.1.0 rpi234 arm64)" "kalinka-5.1.0-rpi234-arm64.img"

echo "  -- a resolv.conf that is a file"
mkdir -p "$ROOTFS/etc"
printf 'nameserver 192.0.2.53\n' > "$ROOTFS/etc/resolv.conf"
use_build_dns
assert_eq "the build resolves as the host does" \
  "$(cat "$ROOTFS/etc/resolv.conf")" "$(cat /etc/resolv.conf)"
restore_dns
assert_eq "the image gets its own file back" "$(cat "$ROOTFS/etc/resolv.conf")" "nameserver 192.0.2.53"

echo "  -- a resolv.conf that is a symlink"
rm -f "$ROOTFS/etc/resolv.conf"
ln -s ../run/systemd/resolve/stub-resolv.conf "$ROOTFS/etc/resolv.conf"
use_build_dns
[ -L "$ROOTFS/etc/resolv.conf" ] && fail "the build writes through the link into the image's /run"
restore_dns
assert_eq "the link comes back pointing where it did" \
  "$(readlink "$ROOTFS/etc/resolv.conf")" "../run/systemd/resolve/stub-resolv.conf"

echo "  -- no resolv.conf at all"
rm -f "$ROOTFS/etc/resolv.conf"
use_build_dns
restore_dns
assert_no_file "none is left behind" "$ROOTFS/etc/resolv.conf"

echo "  -- the overlay's ownership"
if [ "$(id -u)" -eq 0 ]; then
  SCRIPT_DIR="$WORK/pkg"
  mkdir -p "$SCRIPT_DIR/overlays/test/etc/issue.d" "$WORK/image/etc"
  echo notice > "$SCRIPT_DIR/overlays/test/etc/issue.d/10-test.issue"
  chown -R 1001:1001 "$SCRIPT_DIR/overlays/test"
  ROOTFS="$WORK/image"
  install_overlay test
  assert_eq "the image root stays root's" "$(stat -c %U "$ROOTFS")" root
  assert_eq "and so does /etc" "$(stat -c %U "$ROOTFS/etc")" root
  assert_eq "and the files it brings" "$(stat -c %U:%G "$ROOTFS/etc/issue.d/10-test.issue")" root:root
  ROOTFS="$WORK/rootfs"
else
  echo "    SKIP: ownership needs root"
fi

echo "  -- the overlay's modes"
SCRIPT_DIR="$WORK/modes"
mkdir -p "$SCRIPT_DIR/overlays/test/etc/new" "$WORK/moded/etc"
printf '#!/bin/sh\n' > "$SCRIPT_DIR/overlays/test/etc/new/hook.sh"
chmod 775 "$SCRIPT_DIR/overlays/test" "$SCRIPT_DIR/overlays/test/etc"
chmod 755 "$SCRIPT_DIR/overlays/test/etc/new/hook.sh" "$WORK/moded" "$WORK/moded/etc"
ROOTFS="$WORK/moded"
install_overlay test
assert_mode "a group-writable checkout leaves the image root as it was" "$ROOTFS" 755
assert_mode "and /etc" "$ROOTFS/etc" 755
[ -x "$ROOTFS/etc/new/hook.sh" ] || fail "a script loses its execute bit"
ROOTFS="$WORK/rootfs"

echo "  -- enabled units, whose links are absolute"
ROOTFS="$WORK/units"
mkdir -p "$ROOTFS/etc/systemd/system/multi-user.target.wants"
ln -s /etc/systemd/system/kalinka-test-absent.service \
  "$ROOTFS/etc/systemd/system/multi-user.target.wants/kalinka-test-absent.service"
( require_enabled multi-user.target kalinka-test-absent.service ) 2>/dev/null \
  || fail "missed an enabled unit whose link does not resolve on the build host"
( require_disabled multi-user.target kalinka-test-absent.service ) 2>/dev/null \
  && fail "took a unit whose link does not resolve on the build host for disabled"
( require_disabled multi-user.target never-enabled.service ) \
  || fail "refused a unit that is not enabled"
ROOTFS="$WORK/rootfs"

echo "  -- which refused packages this build brought in"
printf 'bash\nbash-completion\ncoreutils\n' > "$WORK/before"
printf 'bash\nbash-completion\ncoreutils\nlibgl1\npython3\n' > "$WORK/after"
assert_eq "one the base already had does not count" \
  "$(packages_landed "$WORK/before" "$WORK/after" bash-completion libgl1 modemmanager)" "libgl1"
printf 'bash\n' > "$WORK/after"
assert_eq "nothing new, nothing reported" \
  "$(packages_landed "$WORK/before" "$WORK/after" bash-completion libgl1)" ""
assert_eq "and the build, which runs under set -e, goes on" \
  "$(set -e; packages_landed "$WORK/before" "$WORK/after" libgl1; echo carried-on)" "carried-on"
printf 'bash\nlibnvidia-cfg1\nnvidia-support\nnvidia-tesla-535-vdpau-driver\n' > "$WORK/after"
assert_eq "a glob names a whole family" \
  "$(packages_landed "$WORK/before" "$WORK/after" libgl1 'nvidia-*' | tr '\n' ' ')" \
  "nvidia-support nvidia-tesla-535-vdpau-driver "

exit "$FAILURES"
