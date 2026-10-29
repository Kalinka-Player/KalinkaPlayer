#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$pkg_dir/lib/common.sh"
. "$pkg_dir/lib/supervisor.sh"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root/etc/bluetooth"
printf '[General]\n#JustWorksRepairing = never\n[Policy]\nAutoEnable=true\n' > "$root/etc/bluetooth/main.conf"
configure_supervisor_radios "$root"
configure_supervisor_radios "$root"
[ "$(grep -c '^JustWorksRepairing = always$' "$root/etc/bluetooth/main.conf")" = 1 ]
grep -q '^AutoEnable=true$' "$root/etc/bluetooth/main.conf"
config="$root/config.txt"
assembled='# Assembled by rpi-image-gen from layer fragments.\ndtparam=audio=on\n#dtoverlay=disable-bt\ndtparam=i2c_arm=on\n'
printf "$assembled" > "$config"
( verify_supervisor_boot_config "$config" )
for line in dtoverlay=disable-bt dtoverlay=disable-wifi ' dtoverlay=disable-bt,foo' dtoverlay=disable-bt-pi5 dtoverlay=disable-wifi-pi5; do
  { printf "$assembled"; printf '%s\n' "$line"; } > "$config"
  if ( verify_supervisor_boot_config "$config" ) 2>/dev/null; then
    echo "Took a config.txt with $line" >&2
    exit 1
  fi
done
for i2c in '' '#dtparam=i2c_arm=on' 'dtparam=i2c_arm=off'; do
  printf '%s\n' "$i2c" > "$config"
  if ( verify_supervisor_boot_config "$config" ) 2>/dev/null; then
    echo "Took a config.txt without the HAT I2C bus: '$i2c'" >&2
    exit 1
  fi
done
ROOTFS="$root/zones"
mkdir -p "$ROOTFS/usr/lib/systemd" "$ROOTFS/usr/share/dbus-1/system-services" \
  "$ROOTFS/usr/share/zoneinfo" "$ROOTFS/etc/systemd/system"
printf '#!/bin/sh\n' > "$ROOTFS/usr/lib/systemd/systemd-timedated"
chmod 755 "$ROOTFS/usr/lib/systemd/systemd-timedated"
printf '[D-BUS Service]\nName=org.freedesktop.timedate1\n' \
  > "$ROOTFS/usr/share/dbus-1/system-services/org.freedesktop.timedate1.service"
printf 'GB\t+513030-0000731\tEurope/London\n' > "$ROOTFS/usr/share/zoneinfo/zone.tab"
( require_timezone_support )
ln -s /dev/null "$ROOTFS/etc/systemd/system/systemd-timedated.service"
if ( require_timezone_support ) 2>/dev/null; then
  echo 'Took an image whose systemd-timedated is masked' >&2
  exit 1
fi
rm "$ROOTFS/etc/systemd/system/systemd-timedated.service"
for missing in usr/lib/systemd/systemd-timedated \
    usr/share/dbus-1/system-services/org.freedesktop.timedate1.service usr/share/zoneinfo/zone.tab; do
  mv "$ROOTFS/$missing" "$root/aside"
  if ( require_timezone_support ) 2>/dev/null; then
    echo "Took an image without $missing" >&2
    exit 1
  fi
  mv "$root/aside" "$ROOTFS/$missing"
done
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
for helper in reinstall ssh-setup soundcard; do
  installed="$root/package/usr/lib/kalinka-supervisor/$helper.sh"
  if [ ! -x "$installed" ]; then
    echo "Missing executable supervisor helper: $helper.sh" >&2
    exit 1
  fi
  cmp "$pkg_dir/../kalinka-supervisor/$helper.sh" "$installed"
done
for shared in dietpi-conf boot-config; do
  cmp "$pkg_dir/../kalinka-supervisor/$shared.sh" "$root/package/usr/lib/kalinka-supervisor/$shared.sh"
done
[ -f "$root/package/usr/lib/systemd/system/kalinka-soundcard.service" ]
grep -q '^    systemctl enable kalinka-soundcard.service || true$' "$deb"
# DietPi on a PC has DietPi's tool but no HAT to load.
grep -qF 'if [ -x /boot/dietpi/func/dietpi-set_hardware ] && [ -f /boot/firmware/config.txt ]; then' "$deb"
grep -q '^    systemctl stop kalinka-soundcard.service || true$' "$deb"
dependencies="$(dpkg-deb --field "$package" Depends)"
for dependency in curl passwd sudo; do
  if ! grep -Eq "(^|, )$dependency(,| |$)" <<< "$dependencies"; then
    echo "Missing supervisor dependency: $dependency" >&2
    exit 1
  fi
done
echo 'Provisioning radios, HAT detection, time zone support, always-on supervisor, packaging and pairing configuration: passed'
