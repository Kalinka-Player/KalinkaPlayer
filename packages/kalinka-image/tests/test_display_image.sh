#!/usr/bin/env bash
#
# The display image's own edits: the KMS driver DietPi ships commented out,
# its 16 MB GPU split, and the setting that switches the display on. A
# headless target must come out of the same steps untouched.
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

# The lines of DietPi's RPi config.txt this cares about.
dietpi_config() {
  mkdir -p "$1/boot/firmware"
  cat > "$1/boot/firmware/config.txt" <<'CONF'
#-------GPU memory splits-------
gpu_mem_256=16
gpu_mem_512=16
gpu_mem_1024=16

#-------Display---------
hdmi_blanking=1

# Enable KMS/DRM display driver, recommended when a GUI application or desktop is used.
#dtoverlay=vc4-kms-v3d,noaudio
dtoverlay=hifiberry-dac
CONF
}

conf_json() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))' "$1"; }

echo "  -- a headless target"
root="$WORK/headless"
dietpi_config "$root"
before="$(cat "$root/boot/firmware/config.txt")"
TARGET_DISPLAY=0
configure_display_setting "$root"
configure_display_boot "$root"
assert_eq "config.txt is untouched" "$(cat "$root/boot/firmware/config.txt")" "$before"
assert_no_file "no settings are written" "$root/etc/kalinka/kalinka_conf.cfg"

echo "  -- a display target"
TARGET_DISPLAY=1
root="$WORK/display"
dietpi_config "$root"
configure_display_setting "$root"
configure_display_boot "$root"
config="$root/boot/firmware/config.txt"
assert_eq "DietPi's own KMS line is the one switched on" \
  "$(grep -c '^dtoverlay=vc4-kms-v3d,noaudio$' "$config")" 1
assert_eq "no commented copy is left behind" "$(grep -c 'vc4-kms-v3d' "$config")" 1
assert_eq "the 16 MB GPU split is gone" "$(grep -c 'gpu_mem' "$config")" 0
assert_contains "other overlays stay" "$(cat "$config")" "dtoverlay=hifiberry-dac"
assert_eq "the display is switched on" \
  "$(conf_json "$root/etc/kalinka/kalinka_conf.cfg")" '{"base_config.display.enabled": true}'
assert_mode "the settings are private" "$root/etc/kalinka/kalinka_conf.cfg" 600

snapshot="$(cat "$config" "$root/etc/kalinka/kalinka_conf.cfg")"
configure_display_setting "$root"
configure_display_boot "$root"
assert_eq "running it again changes nothing" \
  "$(cat "$config" "$root/etc/kalinka/kalinka_conf.cfg")" "$snapshot"

echo "  -- settings that are already there"
root="$WORK/existing"
mkdir -p "$root/etc/kalinka"
printf '{"base_config.display.rotation": "90"}\n' > "$root/etc/kalinka/kalinka_conf.cfg"
configure_display_setting "$root"
assert_eq "they are kept" "$(conf_json "$root/etc/kalinka/kalinka_conf.cfg")" \
  '{"base_config.display.enabled": true, "base_config.display.rotation": "90"}'

echo "  -- a config.txt without DietPi's KMS line"
root="$WORK/bare"
mkdir -p "$root/boot/firmware"
printf '[all]\ndtparam=audio=off\n' > "$root/boot/firmware/config.txt"
configure_display_boot "$root"
configure_display_boot "$root"
assert_eq "the driver is added once, for every board" \
  "$(tail -n 2 "$root/boot/firmware/config.txt")" $'[all]\ndtoverlay=vc4-kms-v3d,noaudio'
assert_eq "exactly one KMS line" \
  "$(grep -c 'vc4-kms-v3d' "$root/boot/firmware/config.txt")" 1

echo "  -- display targets"
for target in rpi234 rpi5; do
  assert_eq "$target-display builds on the same DietPi image as $target" \
    "$(. "$PKG_DIR/targets/$target-display.sh"; echo "$DIETPI_IMAGE $TARGET_ARCH")" \
    "$(. "$PKG_DIR/targets/$target.sh"; echo "$DIETPI_IMAGE $TARGET_ARCH")"
  assert_eq "$target-display has the display" \
    "$(. "$PKG_DIR/targets/$target-display.sh"; echo "${TARGET_DISPLAY:-0}")" 1
  assert_eq "$target does not" "$(unset TARGET_DISPLAY; . "$PKG_DIR/targets/$target.sh"; echo "${TARGET_DISPLAY:-0}")" 0
done

exit "$FAILURES"
