#!/usr/bin/env bash
#
# The display image's own edit, the setting that switches the display on; the
# installer switches on its driver. A headless target must come out untouched.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common display; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

conf_json() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))' "$1"; }

echo "  -- a headless target"
root="$WORK/headless"
TARGET_DISPLAY=0
configure_display_setting "$root"
assert_no_file "no settings are written" "$root/etc/kalinka/kalinka_conf.cfg"

echo "  -- a display target"
TARGET_DISPLAY=1
root="$WORK/display"
configure_display_setting "$root"
assert_eq "the display is switched on" \
  "$(conf_json "$root/etc/kalinka/kalinka_conf.cfg")" '{"base_config.display.enabled": true}'
assert_mode "the settings are private" "$root/etc/kalinka/kalinka_conf.cfg" 600

snapshot="$(cat "$root/etc/kalinka/kalinka_conf.cfg")"
configure_display_setting "$root"
assert_eq "running it again changes nothing" \
  "$(cat "$root/etc/kalinka/kalinka_conf.cfg")" "$snapshot"

echo "  -- settings that are already there"
root="$WORK/existing"
mkdir -p "$root/etc/kalinka"
printf '{"base_config.display.rotation": "90"}\n' > "$root/etc/kalinka/kalinka_conf.cfg"
configure_display_setting "$root"
assert_eq "they are kept" "$(conf_json "$root/etc/kalinka/kalinka_conf.cfg")" \
  '{"base_config.display.enabled": true, "base_config.display.rotation": "90"}'

echo "  -- the display driver"
config="$WORK/config.txt"
printf 'dtparam=audio=on\n[pi4]\ndtoverlay=vc4-kms-v3d\n' > "$config"
assert_eq "a display target accepts the KMS driver" \
  "$(TARGET_DISPLAY=1; verify_display_driver "$config" 2>&1 && echo ok)" ok
printf 'dtparam=audio=on\n#dtoverlay=vc4-kms-v3d,noaudio\n' > "$config"
assert_contains "a display target refuses a commented-out driver" \
  "$(TARGET_DISPLAY=1; verify_display_driver "$config" 2>&1)" 'the KMS display driver is off'
assert_eq "a headless target does not ask for it" \
  "$(TARGET_DISPLAY=0; verify_display_driver "$config" 2>&1 && echo ok)" ok

exit "$FAILURES"
