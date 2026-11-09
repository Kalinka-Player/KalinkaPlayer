# shellcheck shell=bash
# Raspberry Pi Imager's settings, applied at the first start that finds them on the card. Defines only.

CARD_SETTINGS_PROGRAM=/usr/lib/kalinka-image/card-settings.py
CARD_SETTINGS_UNITS=(kalinka-card-settings.service kalinka-card-network-wait.service)
CARD_SETTINGS_FILES=(
  "$CARD_SETTINGS_PROGRAM"
  /usr/lib/systemd/system/kalinka-card-settings.service
  /usr/lib/systemd/system/kalinka-card-network-wait.service
  /usr/lib/systemd/system/kernel-command-line.target
)
SYSTEMD_RUN_GENERATOR=/etc/systemd/system-generators/systemd-run-generator

install_card_settings() {
  local file
  for file in "${CARD_SETTINGS_FILES[@]}"; do
    install -D -m 644 "$SCRIPT_DIR/overlays/debootstrap$file" "$ROOTFS$file"
  done
  # Imager's systemd.run= would run its own script, written for Raspberry Pi OS, as root.
  install -d "$ROOTFS${SYSTEMD_RUN_GENERATOR%/*}"
  ln -sfn /dev/null "$ROOTFS$SYSTEMD_RUN_GENERATOR"
  in_chroot systemctl enable "${CARD_SETTINGS_UNITS[@]}"
}

verify_card_settings() {
  require_enabled multi-user.target "${CARD_SETTINGS_UNITS[@]}"
  [ "$(readlink "$ROOTFS$SYSTEMD_RUN_GENERATOR")" = /dev/null ] \
    || die "systemd.run= on a card from Imager would still run Imager's script"
  grep -qx 'Requires=default.target' \
    "$ROOTFS/usr/lib/systemd/system/kernel-command-line.target" 2>/dev/null \
    || die "a card from Imager would start in rescue.target"
  local tool
  for tool in /usr/bin/nm-online /usr/sbin/useradd /usr/sbin/usermod /usr/sbin/chpasswd /usr/sbin/iw; do
    [ -x "$ROOTFS$tool" ] || die "$tool is missing, which the card's settings need"
  done
  # The image's own Python, on a root with no card: nothing to apply, but every import made.
  in_chroot python3 -I "$CARD_SETTINGS_PROGRAM" --root /nonexistent \
    || die "the card settings program does not run in this image"
  if [ -x "$ROOTFS/usr/bin/systemd-analyze" ]; then
    in_chroot systemd-analyze verify --recursive-errors=no --man=no \
      "${CARD_SETTINGS_FILES[@]:1}" || die "systemd finds fault with the card settings units"
  fi
}
