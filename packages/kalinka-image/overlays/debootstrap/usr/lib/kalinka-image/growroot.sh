#!/usr/bin/env bash
#
# Make the root filesystem fill its partition, and the partition fill its
# disk. The image is built at a fixed size so it stays small to publish and
# quick to flash; everything past that size is unallocated until this runs.
#
# Idempotent, and run on every boot rather than once: moving the card into a
# larger one is then just a reboot away from using the space.
set -euo pipefail

root_source="$(findmnt -no SOURCE /)"
# A symlink such as /dev/disk/by-slot/system has no number in its name.
disk="/dev/$(lsblk -rno PKNAME "$root_source")"
partition="$(lsblk -rno PARTN "$root_source")"

# growpart exits 1 when the partition already reaches the end of the disk,
# which is the normal state on every boot after the first, and 2 on failure.
status=0
growpart "$disk" "$partition" || status=$?
case "$status" in
  0) partx -u "$disk" ;;
  1) ;;
  *) exit "$status" ;;
esac
resize2fs "$root_source"
