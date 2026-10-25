#!/usr/bin/env bash
#
# The Pi images' splash as the kernel's fullscreen_logo reads it, and the
# build's check that a built image carries it.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common splash; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done
splash="$PKG_DIR/rpi-image-gen/splash/kalinka.tga"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "  -- the image"
read -r -a header <<< "$(od -An -v -tu1 -N18 "$splash" | tr '\n' ' ')"
width=$(( header[12] | header[13] << 8 ))
height=$(( header[14] | header[15] << 8 ))
assert_eq 'uncompressed true colour' "${header[2]}" 2
assert_eq 'three bytes a pixel' "${header[16]}" 24
assert_eq 'full HD wide' "$width" 1920
assert_eq 'full HD high' "$height" 1080
pixels="$(tail -c +"$(( 19 + header[0] ))" "$splash" | od -An -v -tx1 -w3)"
assert_eq 'every pixel there' "$(wc -l <<< "$pixels")" "$(( width * height ))"
# The kernel's palette holds 224 colours; a pixel past them is drawn in the first.
colours="$(sort -u <<< "$pixels" | wc -l)"
[ "$colours" -le 224 ] || fail "$colours colours, more than the kernel's 224"
# The kernel fills a screen larger than the image with its top-left pixel: the app's background.
top_left=$(( header[17] & 0x20 ? 1 : (height - 1) * width + 1 ))
assert_eq 'top-left pixel is the app background' "$(sed -n "${top_left}p" <<< "$pixels")" ' 08 08 08'

echo "  -- the built image's check"
root="$WORK/root"
mkdir -p "$root/usr/lib/firmware" "$root/boot/firmware" "$WORK/bin"
cmdline='fsck.repair=yes rootwait root=/dev/disk/by-slot/system fullscreen_logo=1 fullscreen_logo_name=logo.tga vt.global_cursor_default=0 quiet'
# chroot ROOT lsinitramfs FILE prints the listing the case wrote.
printf '#!/bin/sh\ncat "%s/listing"\n' "$WORK" > "$WORK/bin/chroot"
chmod 755 "$WORK/bin/chroot"
built() {
  cp "$splash" "$root/usr/lib/firmware/logo.tga"
  printf '%s\n' "$cmdline" > "$root/boot/firmware/cmdline.txt"
  : > "$root/boot/firmware/initramfs_2712"
  printf 'usr\nusr/lib/firmware\nusr/lib/firmware/logo.tga\n' > "$WORK/listing"
}
verify() { (PATH="$WORK/bin:$PATH"; verify_splash "$root" "$splash" 2>&1 && echo ok); }

built
assert_eq 'an image with the splash passes' "$(verify)" ok
built
{ echo usr/lib/firmware/logo.tga; seq 200000; } > "$WORK/listing"
assert_eq 'an early match in a long initramfs listing passes' "$(verify)" ok
built
printf 'other\n' > "$root/usr/lib/firmware/logo.tga"
assert_contains 'another logo fails' "$(verify)" 'lacks the Kalinka splash'
for arg in fullscreen_logo=1 fullscreen_logo_name=logo.tga vt.global_cursor_default=0 quiet; do
  built
  printf '%s\n' "${cmdline/ $arg/}" > "$root/boot/firmware/cmdline.txt"
  assert_contains "a command line without $arg fails" "$(verify)" "cmdline.txt lacks $arg"
done
built
rm "$root/boot/firmware/initramfs_2712"
assert_contains 'no initramfs fails' "$(verify)" 'no initramfs'
built
printf 'usr\nusr/lib/firmware\n' > "$WORK/listing"
assert_contains 'an initramfs without the logo fails' "$(verify)" 'initramfs_2712 lacks the splash'

exit "$FAILURES"
