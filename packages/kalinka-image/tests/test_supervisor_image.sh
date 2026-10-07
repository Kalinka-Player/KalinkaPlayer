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
# Always on: the process starts nearby setup itself once a radio appears.
grep -q '^WantedBy=multi-user.target$' "$unit"
! grep -q '^Condition\|^ExecCondition' "$unit"
[ ! -e "$pkg_dir/../kalinka-supervisor/systemd/kalinka-wireless.target" ]
[ ! -e "$pkg_dir/../kalinka-supervisor/udev" ]
grep -q '^After=dietpi-preboot.service dietpi-firstboot.service kalinka-firstboot.service bluetooth.service NetworkManager.service$' "$unit"
deb="$pkg_dir/../kalinka-supervisor/build-deb.sh"
grep -q '^  systemctl reenable kalinka-supervisor.service || true$' "$deb"
grep -q '^  systemctl --no-block restart kalinka-supervisor.service || true$' "$deb"
# Reinstall runs from the supervisor's own package, never from Core's installation.
grep -q 'install -m 755 "$pkg_dir/reinstall.sh" "$stage/usr/lib/kalinka-supervisor/"' "$deb"
grep -q '^Depends: .*\bcurl\b' "$deb"
grep -q '^ExecStart=/usr/lib/kalinka-supervisor/reinstall.sh$' "$pkg_dir/../kalinka-supervisor/systemd/kalinka-reinstall.service"
echo 'Provisioning radios, always-on supervisor, packaging and pairing configuration: passed'
