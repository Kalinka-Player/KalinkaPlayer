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
grep -q '^ExecStart=/usr/lib/kalinka-supervisor/reinstall.sh$' "$pkg_dir/../kalinka-supervisor/systemd/kalinka-reinstall.service"

# Compilation is tested separately; exercise the actual package assembly here.
cat > "$root/go" <<'GO'
#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then
    printf '#!/bin/sh\nexit 0\n' > "$2"
    chmod 755 "$2"
    exit 0
  fi
  shift
done
exit 1
GO
chmod 755 "$root/go"
GO="$root/go" GOARCH=amd64 VERSION=0.0.0 OUT_DIR="$root/deb" bash "$deb"
package="$root/deb/kalinka-supervisor_0.0.0_amd64.deb"
dpkg-deb --extract "$package" "$root/package"
for helper in reinstall ssh-setup; do
  installed="$root/package/usr/lib/kalinka-supervisor/$helper.sh"
  if [ ! -x "$installed" ]; then
    echo "Missing executable supervisor helper: $helper.sh" >&2
    exit 1
  fi
  cmp "$pkg_dir/../kalinka-supervisor/$helper.sh" "$installed"
done
dependencies="$(dpkg-deb --field "$package" Depends)"
for dependency in curl passwd sudo; do
  if ! grep -Eq "(^|, )$dependency(,| |$)" <<< "$dependencies"; then
    echo "Missing supervisor dependency: $dependency" >&2
    exit 1
  fi
done
echo 'Provisioning radios, always-on supervisor, packaging and pairing configuration: passed'
