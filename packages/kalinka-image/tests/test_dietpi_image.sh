#!/usr/bin/env bash
#
# The two steps that take a downloaded DietPi image as trustworthy and make
# room in it: a signature from the pinned key and nobody else, and a grown root
# partition the Pi's boot chain still finds by the same disk id.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck disable=SC2034  # base-dietpi.sh reads it
SCRIPT_DIR="$PKG_DIR"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/lib/common.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/lib/base-dietpi.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "  -- growing the root partition"
image="$WORK/dietpi.img"
truncate -s 64MiB "$image"
sfdisk --quiet "$image" <<'LAYOUT'
label: dos
label-id: 0x1234abcd
start=2048, size=16384, type=c, bootable
start=18432, size=32768, type=83
LAYOUT
( grow_root_partition "$image" 128MiB ) || fail "refused a two-partition MBR image"
table="$(sfdisk -d "$image")"
assert_eq "keeps the disk id the boot chain names" "$(sfdisk --disk-id "$image")" 0x1234abcd
assert_contains "leaves the boot partition as it was" "$table" "start=        2048, size=       16384, type=c, bootable"
assert_contains "and grows the root to the end" "$table" "start=       18432, size=      243712, type=83"
( grow_root_partition "$image" 64MiB ) || fail "refused an image already bigger than asked"
assert_eq "never shrinks the image" "$(stat -c %s "$image")" $((128 * 1024 * 1024))

truncate -s 64MiB "$WORK/gpt.img"
printf 'label: gpt\n,16MiB,U\n,,L\n' | sfdisk --quiet "$WORK/gpt.img"
( grow_root_partition "$WORK/gpt.img" 128MiB ) 2>/dev/null && fail "grew a GPT image"
truncate -s 64MiB "$WORK/three.img"
printf 'label: dos\n,8MiB,c\n,8MiB,83\n,,83\n' | sfdisk --quiet "$WORK/three.img"
( grow_root_partition "$WORK/three.img" 128MiB ) 2>/dev/null && fail "grew an image with three partitions"

echo "  -- the signature"
if ! command -v gpg >/dev/null || ! command -v gpgv >/dev/null; then
  echo "    SKIP: needs gpg and gpgv"
  exit "$FAILURES"
fi
export GNUPGHOME="$WORK/gnupg"
mkdir -m 700 "$GNUPGHOME"
new_key() {
  gpg --batch --quiet --passphrase '' --quick-gen-key "$1 <$1@example.com>" ed25519 sign never 2>/dev/null
  gpg --list-keys --with-colons "$1@example.com" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }'
}
pinned="$(new_key pinned)"
other="$(new_key other)"
if [ -z "$pinned" ] || [ -z "$other" ]; then
  fail "could not make the test keys (is gpg-agent installed?)"
  exit "$FAILURES"
fi
gpg --armor --export "$pinned" > "$WORK/pinned.asc"
gpg --armor --export "$pinned" "$other" > "$WORK/both.asc"
echo "an image" > "$WORK/image.xz"
gpg --batch --quiet --local-user "$pinned" --detach-sign --armor -o "$WORK/good.asc" "$WORK/image.xz"
gpg --batch --quiet --local-user "$other" --detach-sign --armor -o "$WORK/other.asc" "$WORK/image.xz"

( verify_signature "$WORK/pinned.asc" "$pinned" "$WORK/image.xz" "$WORK/good.asc" ) \
  || fail "rejected a good signature from the pinned key"
echo "a changed image" > "$WORK/tampered.xz"
( verify_signature "$WORK/pinned.asc" "$pinned" "$WORK/tampered.xz" "$WORK/good.asc" ) 2>/dev/null \
  && fail "accepted a signature over different bytes"
( verify_signature "$WORK/both.asc" "$pinned" "$WORK/image.xz" "$WORK/other.asc" ) 2>/dev/null \
  && fail "accepted a good signature from a key that is not the pinned one"

exit "$FAILURES"
