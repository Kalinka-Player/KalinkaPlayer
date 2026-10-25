# shellcheck shell=bash
# The Pi images' boot splash, drawn by the kernel's fullscreen_logo. Defines only.

# Usage: verify_splash ROOT IMAGE   once rpi-image-gen has written the boot files: the splash is IMAGE, in every initramfs, and the kernel is told to draw it.
verify_splash() {
  local root="$1" image="$2" cmdline arg initramfs
  local -a initramfses=("$root"/boot/firmware/initramfs*)
  cmp -s "$image" "$root/usr/lib/firmware/logo.tga" || die 'the image lacks the Kalinka splash'
  cmdline=" $(cat "$root/boot/firmware/cmdline.txt") "
  # Without quiet, with no serial console to take them, kernel and systemd messages scroll over the splash.
  for arg in fullscreen_logo=1 fullscreen_logo_name=logo.tga vt.global_cursor_default=0 quiet; do
    [[ $cmdline == *" $arg "* ]] || die "cmdline.txt lacks $arg"
  done
  [ -e "${initramfses[0]}" ] || die 'the image has no initramfs to carry the splash'
  for initramfs in "${initramfses[@]}"; do
    # Not grep -q: under pipefail, lsinitramfs dying of SIGPIPE after an early match would fail the build.
    chroot "$root" lsinitramfs "/boot/firmware/${initramfs##*/}" | grep 'lib/firmware/logo\.tga$' > /dev/null \
      || die "${initramfs##*/} lacks the splash"
  done
}
