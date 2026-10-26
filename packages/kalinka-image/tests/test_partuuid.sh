#!/usr/bin/env bash
#
# The build's check that a Pi image finds its partitions by its disk id, run
# against images laid out as genimage lays them out.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common partuuid; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

id=de289abd
cmdline="fsck.repair=yes rootwait root=PARTUUID=$id-02 quiet"
fstab="PARTUUID=$id-02  /  ext4 rw,relatime,errors=remount-ro,commit=30 0 1
PARTUUID=$id-01  /boot/firmware  vfat defaults,rw,noatime,errors=remount-ro 0 2"

# Writes $WORK/kalinka.img with disk id $id, $1 as the boot partition's cmdline.txt and $2 as the root's /etc/fstab.
image() {
  rm -rf "$WORK/kalinka.img" "$WORK/boot.vfat" "$WORK/root.ext4" "$WORK/root"
  mkdir -p "$WORK/root/etc"
  truncate -s 24M "$WORK/kalinka.img"
  sfdisk -q "$WORK/kalinka.img" <<EOF
label: dos
label-id: 0x$id
start=2048, size=16384, type=c, bootable
start=18432, type=83
EOF
  truncate -s 8M "$WORK/boot.vfat"
  mkfs.vfat -n BOOT "$WORK/boot.vfat" > /dev/null
  printf '%s\n' "$1" > "$WORK/cmdline.txt"
  mcopy -i "$WORK/boot.vfat" "$WORK/cmdline.txt" ::
  printf '%s\n' "$2" > "$WORK/root/etc/fstab"
  mkfs.ext4 -q -L ROOT -d "$WORK/root" "$WORK/root.ext4" 15M
  dd if="$WORK/boot.vfat" of="$WORK/kalinka.img" bs=512 seek=2048 conv=notrunc status=none
  dd if="$WORK/root.ext4" of="$WORK/kalinka.img" bs=512 seek=18432 conv=notrunc status=none
}
verify() { (verify_partuuid "$WORK/kalinka.img" 2>&1 && echo ok); }

image "$cmdline" "$fstab"
assert_eq 'an image found by its disk id passes' "$(verify)" ok
image "${cmdline/PARTUUID=$id-02//dev/disk/by-slot/system}" "$fstab"
assert_contains 'a root found by slot on the command line fails' "$(verify)" 'cmdline.txt does not find the root'
image "${cmdline/$id/0badf00d}" "$fstab"
assert_contains "another disk's root on the command line fails" "$(verify)" 'cmdline.txt does not find the root'
image "$cmdline" "${fstab/PARTUUID=$id-02//dev/disk/by-slot/system}"
assert_contains 'a root mounted by slot fails' "$(verify)" 'fstab does not mount / '
image "$cmdline" "${fstab/PARTUUID=$id-01//dev/disk/by-slot/boot}"
assert_contains 'a boot partition mounted by slot fails' "$(verify)" 'fstab does not mount /boot/firmware'

exit "$FAILURES"
