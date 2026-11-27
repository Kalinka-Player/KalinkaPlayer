#!/usr/bin/env bash
#
# The renderer image's checks: a root with the renderer, what it upgrades
# with and what brings its Ethernet up passes, and one missing a piece,
# carrying what a renderer-only box has no use for, or holding what would
# make every card the same box fails, naming why. Then what a Pi 1 boots
# from, and that the image's own layers and hooks are in place.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common kalinka card-settings renderer-image; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOTFS="$WORK/root"

enable() {
  local wants="$1" unit
  shift
  mkdir -p "$ROOTFS/etc/systemd/system/$wants.wants"
  for unit in "$@"; do
    ln -sfn "/usr/lib/systemd/system/$unit" "$ROOTFS/etc/systemd/system/$wants.wants/$unit"
  done
}

# Fails the test unless verify_renderer_image refuses the root, naming why.
refuses() {
  local message
  if message="$(verify_renderer_image 2>&1)"; then
    fail "took a root $1"
  else
    assert_contains "refuses a root $1" "$message" "$2"
  fi
}

TOOLS=(curl python3 apt-get dpkg-deb)
MULTI_USER_UNITS=(kalinka-renderer.service kalinka-renderer-upgrade.path systemd-networkd.service)
SYSINIT_UNITS=(systemd-timesyncd.service systemd-resolved.service)
NETWORK="$ROOTFS/etc/systemd/network"
LINK_MASKS=(99-default.link 73-usb-net-by-mac.link)

# What rpi-image-gen's device layer writes.
network_files() {
  mkdir -p "$NETWORK"
  printf '[Match]\nName=%s\n\n[Network]\nDHCP=%s\n' eth0 "${1:-yes}" > "$NETWORK/01-eth0.network"
  printf '[Match]\nName=wlan0\n\n[Network]\nDHCP=yes\n' > "$NETWORK/02-wlan0.network"
}

echo "  -- a renderer's root"
SCRIPT_DIR="$PKG_DIR"
mkdir -p "$ROOTFS/var/lib/dpkg/info" "$ROOTFS/usr/bin" "$ROOTFS/opt/kalinka" "$ROOTFS/etc"
: > "$ROOTFS/var/lib/dpkg/info/kalinka-renderer.list"
for tool in "${TOOLS[@]}"; do
  printf '#!/bin/sh\n' > "$ROOTFS/usr/bin/$tool"
  chmod 755 "$ROOTFS/usr/bin/$tool"
done
printf '#!/bin/sh\n' > "$ROOTFS/opt/kalinka/upgrade-renderer.sh"
chmod 755 "$ROOTFS/opt/kalinka/upgrade-renderer.sh"
enable multi-user.target "${MULTI_USER_UNITS[@]}"
enable sysinit.target "${SYSINIT_UNITS[@]}"
network_files
for link in "${LINK_MASKS[@]}"; do
  ln -s /dev/null "$NETWORK/$link"
done
ln -s ../run/systemd/resolve/stub-resolv.conf "$ROOTFS/etc/resolv.conf"
: > "$ROOTFS/etc/machine-id"
ignore_imager_first_run
message="$(verify_renderer_image 2>&1)" || fail "refused a renderer's root: $message"
printf 'uninitialized\n' > "$ROOTFS/etc/machine-id"
message="$(verify_renderer_image 2>&1)" || fail "refused a machine-id left for the first boot: $message"
network_files ipv4
message="$(verify_renderer_image 2>&1)" || fail "refused DHCP for IPv4 alone: $message"
network_files

echo "  -- a root missing a piece"
rm "$ROOTFS/var/lib/dpkg/info/kalinka-renderer.list"
refuses "without the renderer" "the renderer is not installed"
: > "$ROOTFS/var/lib/dpkg/info/kalinka-renderer.list"
for unit in "${MULTI_USER_UNITS[@]}"; do
  rm "$ROOTFS/etc/systemd/system/multi-user.target.wants/$unit"
  refuses "where $unit is not enabled" "$unit is not enabled in multi-user.target"
  enable multi-user.target "$unit"
done
for unit in "${SYSINIT_UNITS[@]}"; do
  rm "$ROOTFS/etc/systemd/system/sysinit.target.wants/$unit"
  refuses "where $unit is not enabled" "$unit is not enabled in sysinit.target"
  enable sysinit.target "$unit"
done
mv "$ROOTFS/opt/kalinka/upgrade-renderer.sh" "$WORK/upgrade"
refuses "without the upgrade script" "the renderer's upgrade script is missing"
mv "$WORK/upgrade" "$ROOTFS/opt/kalinka/upgrade-renderer.sh"
for tool in "${TOOLS[@]}"; do
  mv "$ROOTFS/usr/bin/$tool" "$WORK/tool"
  refuses "without $tool" "$tool is missing"
  mv "$WORK/tool" "$ROOTFS/usr/bin/$tool"
done

echo "  -- a root whose Ethernet would bring no address up"
for link in "${LINK_MASKS[@]}"; do
  rm "$NETWORK/$link"
  refuses "where $link names the Ethernet by its MAC" "$link is not masked"
  printf '[Link]\nNamePolicy=mac\n' > "$NETWORK/$link"
  refuses "where $link is a file of its own" "$link is not masked"
  rm "$NETWORK/$link"
  ln -s /dev/null "$NETWORK/$link"
done
rm "$NETWORK/01-eth0.network"
refuses "with a network file for wlan0 alone" "no network file asks for an address on eth0 by DHCP"
network_files no
refuses "that gives eth0 no DHCP" "no network file asks for an address on eth0 by DHCP"
network_files
rm "$ROOTFS/etc/resolv.conf"
printf 'nameserver 192.0.2.1\n' > "$ROOTFS/etc/resolv.conf"
refuses "whose resolv.conf is the build host's" "does not point at systemd-resolved"
rm "$ROOTFS/etc/resolv.conf"
refuses "without a resolv.conf" "does not point at systemd-resolved"
ln -s ../run/systemd/resolve/stub-resolv.conf "$ROOTFS/etc/resolv.conf"
message="$(verify_renderer_image 2>&1)" || fail "refused the network once mended: $message"

echo "  -- a root that would run Imager's settings"
rm "$ROOTFS$SYSTEMD_RUN_GENERATOR"
refuses "that would run Imager's first-run script" "would still run Imager's script"
ignore_imager_first_run
mv "$ROOTFS$IMAGER_TARGET" "$WORK/target"
refuses "without Imager's target" "would start in rescue.target"
ignore_imager_first_run

echo "  -- a root carrying more than a renderer"
for package in kalinka-server kalinka-supervisor:armhf network-manager openssh-server; do
  : > "$ROOTFS/var/lib/dpkg/info/$package.list"
  refuses "with $package" "${package%%:*} came in"
  rm "$ROOTFS/var/lib/dpkg/info/$package.list"
done
: > "$ROOTFS/usr/bin/arm-linux-gnueabihf-gcc-14"
refuses "with a compiler" "came in, though nothing on the box builds from source"
rm "$ROOTFS/usr/bin/arm-linux-gnueabihf-gcc-14"

echo "  -- a root that would make every card the same box"
mkdir -p "$ROOTFS/var/lib/kalinka-renderer"
printf '0b0c5a3e-8c1b-4a51-9a0d-6c9b1c7f1e2a\n' > "$ROOTFS/var/lib/kalinka-renderer/renderer_id"
refuses "with a renderer id" "the image carries a renderer id"
rm "$ROOTFS/var/lib/kalinka-renderer/renderer_id"
printf '4c3f1b0e9d2a4f6b8e7c5a1d3b9f0e2c\n' > "$ROOTFS/etc/machine-id"
refuses "with a machine-id" "the image carries a machine-id"
: > "$ROOTFS/etc/machine-id"
mkdir -p "$ROOTFS/etc/ssh"
: > "$ROOTFS/etc/ssh/ssh_host_ed25519_key"
refuses "with SSH host keys" "the image carries SSH host keys"
rm "$ROOTFS/etc/ssh/ssh_host_ed25519_key"
message="$(verify_renderer_image 2>&1)" || fail "refused the root once mended: $message"

echo "  -- what a Pi 1 boots from"
BOOT_ROOT="$WORK/boot-root"
BOOT="$BOOT_ROOT/boot/firmware"
BOOT_FILES=(bootcode.bin start.elf fixup.dat kernel.img cmdline.txt
            bcm2708-rpi-b.dtb bcm2708-rpi-b-plus.dtb bcm2708-rpi-b-rev1.dtb)
mkdir -p "$BOOT"
for file in "${BOOT_FILES[@]}"; do
  printf 'x' > "$BOOT/$file"
done
printf 'arm_boost=1\ndtparam=audio=on\n' > "$BOOT/config.txt"
message="$(verify_pi1_boot "$BOOT_ROOT" 2>&1)" || fail "refused a Pi 1's boot partition: $message"
for file in "${BOOT_FILES[@]}"; do
  mv "$BOOT/$file" "$WORK/file"
  if message="$(verify_pi1_boot "$BOOT_ROOT" 2>&1)"; then
    fail "took a boot partition without $file"
  else
    assert_contains "refuses one without $file" "$message" "has no $file"
  fi
  mv "$WORK/file" "$BOOT/$file"
done
printf 'arm_boost=1\n' > "$BOOT/config.txt"
if message="$(verify_pi1_boot "$BOOT_ROOT" 2>&1)"; then
  fail "took a config.txt that leaves the Pi's own audio off"
else
  assert_contains "refuses the Pi's own audio off" "$message" "leaves the Pi's own audio off"
fi

echo "  -- the image's layers and hooks"
GEN="$PKG_DIR/rpi-image-gen"
config="$(cat "$GEN/config/kalinka-rpi1-renderer.yaml")"
declared=" $(sed -n 's/^# X-Env-Layer-Name: //p' "$GEN"/layer/*.yaml | tr '\n' ' ') "
for layer in kalinka-rpi1 kalinka-renderer-base kalinka-renderer; do
  assert_contains "the config names $layer" "$config" "$layer"
  assert_contains "$layer is one of ours" "$declared" " $layer "
done
assert_contains "the device takes the v6 kernel" "$(cat "$GEN/layer/kalinka-rpi1.yaml")" \
  "Requires: rpi-device-base,kalinka-rpi-linux-v6"
assert_contains "which is a layer of ours" "$declared" " kalinka-rpi-linux-v6 "
assert_contains "the config runs the renderer's hook" "$config" "hook: \${@SRCROOT}/kalinka-renderer-hook.sh"
[ -x "$GEN/kalinka-renderer-hook.sh" ] || fail "the renderer's hook is not executable"
hook="$(cat "$GEN/kalinka-renderer-hook.sh")"
[[ "$hook" == *$'\nstart_build_aids\ninstall_renderer_only '*$'\nstop_build_aids\nignore_imager_first_run\n'* ]] \
  || fail "the renderer's hook does not install it with the build aids in place, and Imager's first run ignored"
LATE="$GEN/layer/kalinka-renderer.d/hooks/late"
[ -x "$LATE/preimage90-renderer" ] || fail "the renderer image's root is not checked"
late="$(cat "$LATE/preimage90-renderer")"
assert_contains "the late hook checks the root" "$late" $'\nverify_renderer_image\n'
assert_contains "and what a Pi 1 boots from" "$late" 'verify_pi1_boot "$ROOTFS"'
assert_eq "the partition check is the full images'" \
  "$(readlink -f "$LATE/postimage90-partuuid")" \
  "$(readlink -f "$GEN/layer/kalinka.d/hooks/late/postimage90-partuuid")"

exit "$FAILURES"
