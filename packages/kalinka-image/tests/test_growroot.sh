#!/usr/bin/env bash
#
# growroot.sh turns "the filesystem I am mounted on" into a disk plus a
# partition number. Getting that wrong resizes the wrong partition, so it is
# checked against both device naming schemes the image can land on.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
GROWROOT="$PKG_DIR/overlays/debootstrap/usr/lib/kalinka-image/growroot.sh"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"

# Runs growroot with the storage stack faked out: $1 is what findmnt reports
# for /, $2 the disk lsblk names, $3 the exit status growpart returns.
run_growroot() {
  local root_source="$1" disk="$2" growpart_status="$3"
  local bin log
  bin="$(mktemp -d)"
  log="$bin/calls"
  : > "$log"

  cat > "$bin/findmnt" <<SCRIPT
#!/bin/sh
echo "$root_source"
SCRIPT
  cat > "$bin/lsblk" <<SCRIPT
#!/bin/sh
echo "$disk"
SCRIPT
  cat > "$bin/growpart" <<SCRIPT
#!/bin/sh
echo "growpart \$*" >> "$log"
exit $growpart_status
SCRIPT
  local tool
  for tool in partx resize2fs; do
    cat > "$bin/$tool" <<SCRIPT
#!/bin/sh
echo "$tool \$*" >> "$log"
SCRIPT
  done
  chmod 755 "$bin"/*

  PATH="$bin:$PATH" bash "$GROWROOT" >/dev/null 2>&1
  RUN_STATUS=$?
  CALLS="$(cat "$log")"
  rm -rf "$bin"
}

echo "  -- an SD card (mmcblk)"
run_growroot /dev/mmcblk0p2 mmcblk0 0
assert_eq "succeeds" "$RUN_STATUS" 0
assert_contains "grows the right partition" "$CALLS" "growpart /dev/mmcblk0 2"
assert_contains "rereads the table" "$CALLS" "partx -u /dev/mmcblk0"
assert_contains "resizes the filesystem" "$CALLS" "resize2fs /dev/mmcblk0p2"

echo "  -- an SSD or virtual disk (sd)"
run_growroot /dev/sda3 sda 0
assert_contains "grows the right partition" "$CALLS" "growpart /dev/sda 3"
assert_contains "resizes the filesystem" "$CALLS" "resize2fs /dev/sda3"

echo "  -- an NVMe drive"
run_growroot /dev/nvme0n1p3 nvme0n1 0
assert_contains "grows the right partition" "$CALLS" "growpart /dev/nvme0n1 3"
assert_contains "resizes the filesystem" "$CALLS" "resize2fs /dev/nvme0n1p3"

echo "  -- media the image already fills"
run_growroot /dev/mmcblk0p2 mmcblk0 1
assert_eq "survives growpart refusing to change anything" "$RUN_STATUS" 0
assert_not_contains "does not reread an unchanged table" "$CALLS" "partx"
assert_contains "still resizes the filesystem" "$CALLS" "resize2fs /dev/mmcblk0p2"

exit "$FAILURES"
