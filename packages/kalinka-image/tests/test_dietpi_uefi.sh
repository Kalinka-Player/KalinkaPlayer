#!/usr/bin/env bash
set -euo pipefail
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TESTS_DIR")"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/base-dietpi.sh"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
image="$stage/uefi.img"
truncate -s 64MiB "$image"
printf 'label: gpt\n,8MiB,U\n,32MiB,L\n,1MiB,EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n' | sfdisk --quiet "$image"
truncate -s 1MiB "$stage/setup.fat"
mkfs.vfat -n DIETPISETUP "$stage/setup.fat" >/dev/null
start="$(sfdisk --json "$image" | python3 -c 'import json,sys; print(json.load(sys.stdin)["partitiontable"]["partitions"][2]["start"])')"
dd if="$stage/setup.fat" of="$image" bs=512 seek="$start" conv=notrunc status=none
sfdisk --json "$image" > "$stage/before.json"
grow_uefi_partition "$image" 128MiB
sfdisk --json "$image" > "$stage/after.json"
python3 - "$stage" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
a, b = (json.loads((root / f'{name}.json').read_text())['partitiontable'] for name in ('before', 'after'))
assert a['id'] == b['id']
assert a['partitions'][0] == b['partitions'][0]
assert [p['uuid'] for p in a['partitions']] == [p['uuid'] for p in b['partitions']]
assert a['partitions'][1]['start'] == b['partitions'][1]['start']
assert b['partitions'][1]['size'] > a['partitions'][1]['size']
p = b['partitions'][2]
assert b['partitions'][1]['start'] + b['partitions'][1]['size'] == p['start']
with (root / 'uefi.img').open('rb') as f:
    f.seek(p['start'] * 512)
    assert f.read(p['size'] * 512) == (root / 'setup.fat').read_bytes()
PY
grow_uefi_partition "$image" 64MiB
[ "$(stat -c %s "$image")" = "$((128 * 1024 * 1024))" ]
if ( grow_uefi_partition "$stage/setup.fat" 128MiB ) 2>/dev/null; then
  echo 'Accepted a non-GPT image' >&2
  exit 1
fi
echo 'DietPi UEFI growth preserves setup content and partition identities'
