# shellcheck shell=bash
# The now-playing display (kalinka-kiosk) for TARGET_DISPLAY=1 targets. Defines only.

KMS_OVERLAY_RE='^[[:blank:]]*dtoverlay=vc4-kms-v3d([,[:blank:]].*)?$'

display_target() { [ "${TARGET_DISPLAY:-0}" = 1 ]; }

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
  python3 - "$ROOTFS/etc/kalinka/kalinka_conf.cfg" <<'PYCHECK' || die 'the display is not switched on'
import json, sys
sys.exit(json.load(open(sys.argv[1])).get("base_config.display.enabled") is not True)
PYCHECK
  [ "$(in_chroot stat -c '%U %a' /etc/kalinka/kalinka_conf.cfg)" = 'kalusr 600' ] \
    || die 'the server cannot read its settings'
}

# Apart from verify_display: rpi-image-gen writes config.txt only after Kalinka is in.
verify_display_driver() {
  display_target || return 0
  grep -Eq "$KMS_OVERLAY_RE" "$1" || die 'the KMS display driver is off'
}
