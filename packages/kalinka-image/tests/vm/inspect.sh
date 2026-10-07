#!/usr/bin/env bash
#
# Verify DietPi first boot, disk growth and the installed setup service.
# Copy the guest journal out before checks so a failure can be investigated.
#
# Usage: sudo inspect.sh <disk.img> <journal-dir>
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=../helpers.sh
. "$TESTS_DIR/helpers.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/lib/common.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/targets/amd64.sh"

die() { echo "inspect: $*" >&2; exit 1; }

[ $# -eq 2 ] || die "usage: inspect.sh <disk.img> <journal-dir>"
DISK="$1" JOURNAL_OUT="$2"
[ -r "$DISK" ] || die "cannot read $DISK"
require_root

start_work
IMAGE="$DISK"
attach_image "$TARGET_BOOT_PART" "$TARGET_ROOT_PART"
ESP="$WORK/esp"
mkdir -p "$ROOTFS" "$ESP"
mount_at -o ro "$ROOT_DEV" "$ROOTFS" || die "cannot mount the root filesystem"
mount_at -o ro "$BOOT_DEV" "$ESP" || die "cannot mount the ESP"
JOURNAL="$ROOTFS/var/log/journal"

mkdir -p "$JOURNAL_OUT"
if [ -d "$JOURNAL" ]; then
  cp -r "$JOURNAL/." "$JOURNAL_OUT/"
  journalctl --directory "$JOURNAL" --output short-monotonic --no-pager \
    > "$JOURNAL_OUT/journal.txt"
else
  fail "no journal in /var/log/journal"
fi
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$JOURNAL_OUT"

echo "  -- the root partition fills the disk"
disk_sectors="$(blockdev --getsz "$LOOP")"
root_end="$(partx -g -n "$TARGET_ROOT_PART" -o END "$LOOP" | tr -d ' ')"
# Only the backup GPT's 33 sectors may follow the root partition.
unclaimed=$(( (disk_sectors - 1 - root_end) * 512 ))
[ "$unclaimed" -lt $((1024 * 1024)) ] \
  || fail "the root partition stops $((unclaimed / 1024 / 1024)) MiB short of the end of the disk"

echo "  -- the root filesystem fills its partition"
superblock_field() { sed -n "s/^$1:[[:space:]]*//p" <<<"$superblock"; }
if superblock="$(dumpe2fs -h "$ROOT_DEV" 2>/dev/null)"; then
  block_size="$(superblock_field 'Block size')"
  fs_bytes=$(( $(superblock_field 'Block count') * block_size ))
  group_bytes=$(( $(superblock_field 'Blocks per group') * block_size ))
  part_bytes="$(blockdev --getsize64 "$ROOT_DEV")"
  # resize2fs drops a last block group too small to hold its own metadata.
  [ $((part_bytes - fs_bytes)) -lt "$group_bytes" ] \
    || fail "the filesystem uses $((fs_bytes / 1024 / 1024)) MiB of a $((part_bytes / 1024 / 1024)) MiB partition"
else
  fail "dumpe2fs cannot read the root filesystem"
fi

echo "  -- the first-boot units succeeded"
# Partition resizing runs before persistent journaling; disk checks above prove it.
for unit in dietpi-firstboot.service; do
  results="$(journalctl --directory "$JOURNAL" --output cat --output-fields JOB_RESULT \
    UNIT="$unit" JOB_TYPE=start 2>/dev/null)"
  if [ -z "$results" ]; then
    fail "$unit never ran"
  elif grep -qvx done <<<"$results"; then
    fail "$unit ended $(paste -sd, <<<"$results")"
    journalctl --directory "$JOURNAL" --no-pager --unit "$unit"
  fi
done

echo "  -- an initrd"
initrds=("$ROOTFS"/boot/initrd.img-*)
if [ -e "${initrds[0]}" ]; then
  for initrd in "${initrds[@]}"; do
    [ -s "$initrd" ] || fail "${initrd#"$ROOTFS"} is empty"
  done
else
  fail "no /boot/initrd.img-*"
fi

echo "  -- DietPi networking and packaged supervisor"
for package in wpasupplicant ifupdown bluez libpam-systemd kalinka-supervisor; do
  status="$(dpkg-query --admindir="$ROOTFS/var/lib/dpkg" -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null)"
  assert_eq "$package is installed" "$status" "ii "
done
[ "$(readlink "$ROOTFS/etc/systemd/system/systemd-logind.service")" != /dev/null ] || fail 'PC power-button handler is masked'
[ -x "$ROOTFS/boot/dietpi/dietpi-network" ] || fail 'DietPi networking tool is missing'
[ -s "$ESP/EFI/BOOT/BOOTX64.EFI" ] || fail 'no UEFI fallback bootloader'
[ -f "$ROOTFS/usr/lib/systemd/system/kalinka-wifi@.service" ] || fail 'no independent Wi-Fi helper'
exit "$FAILURES"
