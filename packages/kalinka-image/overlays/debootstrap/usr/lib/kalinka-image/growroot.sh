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
disk="/dev/$(lsblk -no PKNAME "$root_source")"
partition="${root_source##*[!0-9]}"

# growpart exits non-zero when the partition already reaches the end of the
# disk, which is the normal state on every boot after the first.
if growpart "$disk" "$partition"; then
  partx -u "$disk"
fi
resize2fs "$root_source"
