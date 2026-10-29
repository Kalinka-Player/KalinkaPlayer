# shellcheck shell=bash
# The supervisor in an image, and what nearby setup needs of the system around it. Defines only.

configure_supervisor_radios() {
  local root="$1"
  rm -f "$root/etc/modprobe.d/dietpi-disable_bluetooth.conf" \
    "$root/etc/modprobe.d/dietpi-disable_wifi.conf"
  mkdir -p "$root/etc/bluetooth"
  # Re-pairing after the phone forgot its bond must work without a screen.
  # The daemon accepts pairing only while offline setup is open.
  local conf="$root/etc/bluetooth/main.conf"
  [ -f "$conf" ] || printf '[General]\n' > "$conf"
  sed -i '/^[[:blank:]#]*JustWorksRepairing[[:blank:]]*=/d' "$conf"
  sed -i '/^\[General\]/a JustWorksRepairing = always' "$conf"
}

build_supervisor() {
  if [ -n "${SUPERVISOR_DEB:-}" ]; then
    [ -f "$SUPERVISOR_DEB" ] || die 'SUPERVISOR_DEB does not exist'
    cp "$SUPERVISOR_DEB" "$WORK/kalinka-supervisor.deb"
  else
    log "Building the supervisor package ($TARGET_ARCH)"
    VERSION="${SUPERVISOR_VERSION:-}" GOARCH="$TARGET_ARCH" \
      OUT_DIR="$WORK" "$SCRIPT_DIR/../kalinka-supervisor/build-deb.sh"
    mv "$WORK"/kalinka-supervisor_*_"$TARGET_ARCH".deb "$WORK/kalinka-supervisor.deb"
  fi
  [ "$(dpkg-deb -f "$WORK/kalinka-supervisor.deb" Package)" = kalinka-supervisor ] || die 'wrong supervisor package'
  [ "$(dpkg-deb -f "$WORK/kalinka-supervisor.deb" Architecture)" = "$TARGET_ARCH" ] || die 'wrong supervisor architecture'
}

install_supervisor() {
  install -m 644 "$WORK/kalinka-supervisor.deb" "$ROOTFS/tmp/kalinka-supervisor.deb"
  in_chroot apt-get install -y --no-install-recommends /tmp/kalinka-supervisor.deb
  rm -f "$ROOTFS/tmp/kalinka-supervisor.deb"
  in_chroot systemctl enable bluetooth.service kalinka-supervisor.service
  configure_supervisor_radios "$ROOTFS"
}

# The supervisor sets the time zone chosen in the app through timedated, which D-Bus starts on demand.
require_timezone_support() {
  [ -x "$ROOTFS/usr/lib/systemd/systemd-timedated" ] || die 'no systemd-timedated to set the time zone with'
  [ -f "$ROOTFS/usr/share/dbus-1/system-services/org.freedesktop.timedate1.service" ] \
    || die 'D-Bus cannot start systemd-timedated'
  [ "$(readlink "$ROOTFS/etc/systemd/system/systemd-timedated.service")" != /dev/null ] \
    || die 'systemd-timedated is masked'
  [ -f "$ROOTFS/usr/share/zoneinfo/zone.tab" ] || die 'no tzdata to choose a time zone from'
}

verify_supervisor() {
  [ "$(in_chroot dpkg-query -W -f='${db:Status-Status}' kalinka-supervisor)" = installed ] || die 'supervisor is not package-managed'
  require_enabled multi-user.target kalinka-supervisor.service
  [ -x "$ROOTFS/usr/lib/kalinka-supervisor/kalinka-supervisor" ] || die 'no supervisor binary'
  in_chroot /usr/lib/kalinka-supervisor/kalinka-supervisor --version
  require_timezone_support
  [ -x "$ROOTFS/usr/lib/kalinka-supervisor/soundcard.sh" ] || die 'no sound card helper'
  [ ! -f "$ROOTFS/etc/modprobe.d/dietpi-disable_bluetooth.conf" ] || die 'Bluetooth is blacklisted'
  [ ! -f "$ROOTFS/etc/modprobe.d/dietpi-disable_wifi.conf" ] || die 'Wi-Fi is blacklisted'
  if [ "$TARGET_BASE" = dietpi ]; then
    grep -q -- '--no-restart' "$ROOTFS/boot/dietpi/dietpi-network" \
      || die 'DietPi base lacks dietpi-network apply --no-restart; refresh the image'
  fi
}

# Apart from verify_supervisor: rpi-image-gen writes config.txt only after the supervisor is in.
verify_supervisor_boot_config() {
  ! grep -Eq '^[[:blank:]]*dtoverlay=disable-(bt|wifi)(-pi5)?(,|[[:blank:]]|$)' "$1" || die 'a setup radio is disabled'
  grep -q '^dtparam=i2c_arm=on$' "$1" || die 'the HAT I2C bus is off'
}
