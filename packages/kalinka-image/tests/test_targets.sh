#!/usr/bin/env bash
#
# Every target names a base that exists, and every debootstrap target's
# partition table is checked by writing it. An index pointing at the wrong
# partition produces an image that builds and does not boot, and sfdisk needs
# no privileges to say so.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"

DEBOOTSTRAP_CONTRACT=(TARGET_PARTITION_LAYOUT TARGET_BOOT_PART TARGET_ROOT_PART
                     TARGET_BOOT_MOUNT TARGET_BOOT_FSTAB_OPTS TARGET_PACKAGES)

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for target_file in "$PKG_DIR"/targets/*.sh; do
  target="$(basename "$target_file" .sh)"
  echo "  -- $target"
  (
    # shellcheck source=/dev/null
    . "$target_file"

    for var in TARGET_BASE TARGET_ARCH; do
      [ -n "${!var:-}" ] || fail "$target: does not set $var"
    done
    [ -r "$PKG_DIR/lib/base-${TARGET_BASE:-}.sh" ] \
      || fail "$target: names a base with no lib/base-${TARGET_BASE:-}.sh"
    [ "${TARGET_BASE:-}" = debootstrap ] || exit "$FAILURES"

    for var in "${DEBOOTSTRAP_CONTRACT[@]}"; do
      [ -n "${!var:-}" ] || fail "$target: does not set $var"
    done
    declare -F target_install_bootloader >/dev/null \
      || fail "$target: does not define target_install_bootloader"

    image="$WORK/$target.img"
    truncate -s 4GiB "$image"
    if ! printf '%s\n' "$TARGET_PARTITION_LAYOUT" | sfdisk --quiet "$image" 2>"$WORK/err"; then
      fail "$target: sfdisk rejected the layout: $(cat "$WORK/err")"
      exit "$FAILURES"
    fi

    table="$(sfdisk --json "$image")"
    boot_type="$(printf '%s' "$table" | python3 -c "
import json,sys
p = json.load(sys.stdin)['partitiontable']['partitions'][$TARGET_BOOT_PART - 1]
print(p['type'])")"
    root_type="$(printf '%s' "$table" | python3 -c "
import json,sys
p = json.load(sys.stdin)['partitiontable']['partitions'][$TARGET_ROOT_PART - 1]
print(p['type'])")"

    # 'c' is W95 FAT32 (LBA) in an MBR table; the GUID is the EFI System
    # Partition. Either way TARGET_BOOT_PART must be the FAT one.
    case "$boot_type" in
      c|C12A7328-F81F-11D2-BA4B-00A0C93EC93B) ;;
      *) fail "$target: partition $TARGET_BOOT_PART is type $boot_type, not FAT" ;;
    esac
    case "$root_type" in
      83|0FC63DAF-8483-4772-8E79-3D69D8477DE4) ;;
      *) fail "$target: partition $TARGET_ROOT_PART is type $root_type, not Linux" ;;
    esac

    # The root partition is the one that grows into the card, so it has to be
    # last; anything after it has nowhere to go.
    count="$(printf '%s' "$table" | python3 -c "
import json,sys
print(len(json.load(sys.stdin)['partitiontable']['partitions']))")"
    assert_eq "$target: root partition is last" "$TARGET_ROOT_PART" "$count"

    size="$(printf '%s' "$table" | python3 -c "
import json,sys
p = json.load(sys.stdin)['partitiontable']['partitions'][$TARGET_BOOT_PART - 1]
print(p['size'] * 512 // 1024 // 1024)")"
    [ "$size" -ge 256 ] \
      || fail "$target: a ${size}MiB boot partition will not hold a kernel and initrd"

    exit "$FAILURES"
  ) || FAILURES=$((FAILURES + $?))
done

exit "$FAILURES"
