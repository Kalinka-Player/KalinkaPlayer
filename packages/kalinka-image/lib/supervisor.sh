# shellcheck shell=bash
# Pi image integration. Avoid running DietPi hardware commands in the build
# chroot: /sys there belongs to the build host, not the eventual appliance.

configure_supervisor_radios() {
  local root="$1"
  if [ -f "$root/boot/firmware/config.txt" ]; then
    sed -i '/^[[:blank:]]*dtoverlay=disable-\(bt\|wifi\)\([,[:blank:]].*\)\?$/d' \
      "$root/boot/firmware/config.txt"
  fi
  if [ -f "$root/boot/firmware/cmdline.txt" ]; then
    # Remove UART consoles before the very first boot, when hciuart starts.
    # DietPi's first-boot serial-console setting cleans up the gettys too.
    sed -Ei ':again; s/(^|[[:blank:]])console=(serial[0-9]+|tty(AMA|S)[0-9]+)(,[^[:blank:]]*)?([[:blank:]]|$)/ /; t again' \
      "$root/boot/firmware/cmdline.txt"
  fi
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
  if [ "${DIETPI_LAYOUT:-}" = rpi ]; then
    in_chroot systemctl enable hciuart.service
    in_chroot systemctl disable serial-getty@ttyAMA0.service
  fi
}

verify_supervisor() {
  [ "$(in_chroot dpkg-query -W -f='${db:Status-Status}' kalinka-supervisor)" = installed ] || die 'supervisor is not package-managed'
  require_enabled multi-user.target kalinka-supervisor.service
  [ -x "$ROOTFS/usr/lib/kalinka-supervisor/kalinka-supervisor" ] || die 'no supervisor binary'
  in_chroot /usr/lib/kalinka-supervisor/kalinka-supervisor --version
  if [ "${DIETPI_LAYOUT:-}" = rpi ]; then
    ! grep -Eq '^[[:blank:]]*dtoverlay=disable-(bt|wifi)(,|[[:blank:]]|$)' \
      "$ROOTFS/boot/firmware/config.txt" || die 'a setup radio is disabled'
    [ ! -f "$ROOTFS/etc/modprobe.d/dietpi-disable_bluetooth.conf" ] || die 'Bluetooth is blacklisted'
    [ ! -f "$ROOTFS/etc/modprobe.d/dietpi-disable_wifi.conf" ] || die 'Wi-Fi is blacklisted'
  fi
  if [ "$TARGET_BASE" = dietpi ]; then
    grep -q -- '--no-restart' "$ROOTFS/boot/dietpi/dietpi-network" \
      || die 'DietPi base lacks dietpi-network apply --no-restart; refresh the image'
  fi
}
