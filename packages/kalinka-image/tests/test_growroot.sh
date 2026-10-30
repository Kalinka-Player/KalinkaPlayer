#!/usr/bin/env bash
#
# growroot.sh turns "the filesystem I am mounted on" into a disk plus a
# partition number. Getting that wrong resizes the wrong partition, so it is
# checked against a root mounted by its device name and one mounted through a
# symlink, as the 3.0.0 Pi images mount /dev/disk/by-slot/system.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
GROWROOT="$PKG_DIR/overlays/debootstrap/usr/lib/kalinka-image/growroot.sh"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"

# Runs growroot with the storage stack faked out: $1 is what findmnt reports
# for /, $2 and $3 the disk and partition number lsblk gives for it, $4 the
# exit status growpart returns.
run_growroot() {
  local root_source="$1" disk="$2" partition="$3" growpart_status="$4"
  local bin log
  bin="$(mktemp -d)"
  log="$bin/calls"
  : > "$log"

  cat > "$bin/findmnt" <<SCRIPT
#!/bin/sh
echo "$root_source"
SCRIPT
  # As the real tools do: lsblk right-aligns numbers outside raw mode, and
  # growpart refuses a partition that is not a bare number.
  cat > "$bin/lsblk" <<SCRIPT
#!/bin/sh
[ "\$3" = "$root_source" ] || exit 32
pad=" "
case "\$1" in *r*) pad= ;; esac
case "\$2" in
  PKNAME) echo "$disk" ;;
  PARTN) echo "\${pad}$partition" ;;
  *) exit 1 ;;
esac
SCRIPT
  cat > "$bin/growpart" <<SCRIPT
#!/bin/sh
echo "growpart \$*" >> "$log"
case "\$2" in ''|*[!0-9]*) exit 2 ;; esac
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
run_growroot /dev/mmcblk0p2 mmcblk0 2 0
assert_eq "succeeds" "$RUN_STATUS" 0
assert_contains "grows the right partition" "$CALLS" "growpart /dev/mmcblk0 2"
assert_contains "rereads the table" "$CALLS" "partx -u /dev/mmcblk0"
assert_contains "resizes the filesystem" "$CALLS" "resize2fs /dev/mmcblk0p2"

echo "  -- a root mounted through a by-slot symlink"
run_growroot /dev/disk/by-slot/system mmcblk0 2 0
assert_eq "succeeds" "$RUN_STATUS" 0
assert_contains "grows the partition behind the link" "$CALLS" "growpart /dev/mmcblk0 2"
assert_contains "resizes the filesystem" "$CALLS" "resize2fs /dev/disk/by-slot/system"

echo "  -- media the image already fills"
run_growroot /dev/mmcblk0p2 mmcblk0 2 1
assert_eq "survives growpart refusing to change anything" "$RUN_STATUS" 0
assert_not_contains "does not reread an unchanged table" "$CALLS" "partx"
assert_contains "still resizes the filesystem" "$CALLS" "resize2fs /dev/mmcblk0p2"

echo "  -- growpart failing"
run_growroot /dev/mmcblk0p2 mmcblk0 2 2
assert_eq "fails the unit" "$RUN_STATUS" 2
assert_not_contains "does not resize the filesystem" "$CALLS" "resize2fs"

exit "$FAILURES"
