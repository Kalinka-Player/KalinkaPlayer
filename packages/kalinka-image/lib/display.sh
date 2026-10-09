# shellcheck shell=bash
# The now-playing display (kalinka-kiosk) for TARGET_DISPLAY=1 targets. Defines only.

KMS_OVERLAY='dtoverlay=vc4-kms-v3d([,[:blank:]].*)?'
KMS_OVERLAY_RE="^[[:blank:]]*$KMS_OVERLAY\$"

display_target() { [ "${TARGET_DISPLAY:-0}" = 1 ]; }

# flutter-pi needs KMS, which DietPi ships commented out beside a 16 MB GPU split.
configure_display_boot() {
  display_target || return 0
  local conf="$1/boot/firmware/config.txt"
  [ -f "$conf" ] || die "no $conf to turn the display driver on in"
  sed -Ei '/^[[:blank:]]*gpu_mem(_[0-9]+)?[[:blank:]]*=/d' "$conf"
  grep -Eq "$KMS_OVERLAY_RE" "$conf" && return 0
  sed -Ei "0,/^[[:blank:]]*#[[:blank:]]*($KMS_OVERLAY)\$/s//\\1/" "$conf"
  grep -Eq "$KMS_OVERLAY_RE" "$conf" || printf '\n[all]\ndtoverlay=vc4-kms-v3d,noaudio\n' >> "$conf"
}

# Before the server installs, so its postinst hands the file to kalusr.
configure_display_setting() {
  display_target || return 0
  install -d "$1/etc/kalinka"
  python3 - "$1/etc/kalinka/kalinka_conf.cfg" <<'PYCONF'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text()) if path.exists() else {}
data["base_config.display.enabled"] = True
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PYCONF
  chmod 600 "$1/etc/kalinka/kalinka_conf.cfg"
}

verify_display() {
  local kiosk
  kiosk="$(in_chroot dpkg-query -W -f='${db:Status-Status}' kalinka-kiosk 2>/dev/null || true)"
  if ! display_target; then
    [ "$kiosk" != installed ] || die 'a headless image has the display installed'
    return 0
  fi
  # install-release.sh only warns when the app release has no kiosk package.
  [ "$kiosk" = installed ] || die 'kalinka-kiosk is not installed'
  [ -x "$ROOTFS/usr/lib/kalinka-kiosk/bundle/flutter-pi" ] || die 'kalinka-kiosk has no flutter-pi'
  require_enabled multi-user.target kalinka-kiosk-sync.path kalinka-kiosk-sync.service
  grep -Eq "$KMS_OVERLAY_RE" "$ROOTFS/boot/firmware/config.txt" || die 'the KMS display driver is off'
  python3 - "$ROOTFS/etc/kalinka/kalinka_conf.cfg" <<'PYCHECK' || die 'the display is not switched on'
import json, sys
sys.exit(json.load(open(sys.argv[1])).get("base_config.display.enabled") is not True)
PYCHECK
  [ "$(in_chroot stat -c '%U %a' /etc/kalinka/kalinka_conf.cfg)" = 'kalusr 600' ] \
    || die 'the server cannot read its settings'
}
