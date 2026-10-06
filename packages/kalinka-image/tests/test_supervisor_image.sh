#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$pkg_dir/lib/supervisor.sh"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root/boot/firmware" "$root/etc/modprobe.d" "$root/etc/bluetooth"
printf '[all]\ndtoverlay=disable-bt\ndtoverlay=disable-wifi\ndtoverlay=hifiberry-dac\n' > "$root/boot/firmware/config.txt"
printf 'console=ttyAMA0,115200 console=serial0,115200 console=tty1 root=PARTUUID=test quiet\n' > "$root/boot/firmware/cmdline.txt"
touch "$root/etc/modprobe.d/dietpi-disable_bluetooth.conf" "$root/etc/modprobe.d/dietpi-disable_wifi.conf"
printf '[General]\n#JustWorksRepairing = never\n[Policy]\nAutoEnable=true\n' > "$root/etc/bluetooth/main.conf"
configure_supervisor_radios "$root"
configure_supervisor_radios "$root"
! grep -q 'disable-' "$root/boot/firmware/config.txt"
grep -q '^dtoverlay=hifiberry-dac$' "$root/boot/firmware/config.txt"
! grep -Eq 'console=(ttyAMA|serial)' "$root/boot/firmware/cmdline.txt"
grep -q 'console=tty1 root=PARTUUID=test quiet' "$root/boot/firmware/cmdline.txt"
[ ! -e "$root/etc/modprobe.d/dietpi-disable_wifi.conf" ]
[ "$(grep -c '^JustWorksRepairing = always$' "$root/etc/bluetooth/main.conf")" = 1 ]
grep -q '^AutoEnable=true$' "$root/etc/bluetooth/main.conf"
unit="$pkg_dir/../kalinka-supervisor/systemd/kalinka-supervisor.service"
! grep -q 'network-online.target' "$unit"
grep -q '^WantedBy=multi-user.target bluetooth.target kalinka-wireless.target$' "$unit"
grep -q '^ConditionPathExistsGlob=/sys/class/net/\*/wireless$' "$unit"
grep -q '^StopWhenUnneeded=yes$' "$pkg_dir/../kalinka-supervisor/systemd/kalinka-wireless.target"
grep -q 'SUBSYSTEM=="net", ENV{DEVTYPE}=="wlan", .*ENV{SYSTEMD_WANTS}+="kalinka-wireless.target"' \
  "$pkg_dir/../kalinka-supervisor/udev/90-kalinka-wireless.rules"
grep -q '^After=dietpi-preboot.service dietpi-firstboot.service kalinka-firstboot.service bluetooth.service NetworkManager.service$' "$unit"
echo 'Provisioning radios, independent service ordering and pairing configuration: passed'
