#!/usr/bin/env bash
#
# The sound-card helper against a staged box. At boot it applies the card
# named in dietpi.txt once, with exactly one restart when the boot
# configuration had to change and never a restart loop. From the supervisor
# it applies one card, says whether that needs a restart, and leaves the
# restart to the supervisor.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=../../kalinka-image/tests/helpers.sh
. "$PKG_DIR/../kalinka-image/tests/helpers.sh"

SOUNDCARD="$PKG_DIR/soundcard.sh"
UNIT="$PKG_DIR/systemd/kalinka-soundcard.service"

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export KALINKA_SOUNDCARD_ROOT="$ROOT"
CONFIG="$ROOT/boot/firmware/config.txt"
STATE="$ROOT/var/lib/kalinka-supervisor/soundcard"
SET_HARDWARE="$ROOT/boot/dietpi/func/dietpi-set_hardware"

# DietPi's dietpi-set_hardware, recording its arguments and, unless told
# otherwise, adding the overlay to config.txt the way the real one does.
dietpi() {
  mkdir -p "$(dirname "$SET_HARDWARE")"
  cat > "$SET_HARDWARE" <<STUB
#!/bin/sh
echo "dietpi-set_hardware G_INTERACTIVE=\$G_INTERACTIVE \$*" >> "\$CALL_LOG"
echo "DietPi-Set_hardware | soundcard \$2"
[ "\${STUB_FAIL:-0}" = 1 ] && exit 1
[ "\${STUB_EDIT:-1}" = 1 ] && echo "dtoverlay=\$2" >> "$CONFIG"
exit 0
STUB
  chmod 755 "$SET_HARDWARE"
  touch "$ROOT/boot/dietpi.txt"
}

no_dietpi() {
  rm -rf "$ROOT/boot/dietpi" "$ROOT/boot/dietpi.txt"
}

# Runs the helper with the given arguments; OUT is its standard output, CALLS what it ran.
run() {
  local saved_path="$PATH"
  make_recorders systemctl sync
  export CALL_LOG
  OUT="$(bash "$SOUNDCARD" "$@" 2>/dev/null)"
  STATUS=$?
  CALLS="$(calls)"
  PATH="$saved_path"
}

boot_with() {
  printf '%b' "$1" > "$ROOT/boot/dietpi.txt"
  run boot
}

config() { cat "$CONFIG"; }

reset_box() {
  rm -rf "${ROOT:?}"/*
  mkdir -p "$ROOT/boot/firmware"
  printf 'dtparam=audio=off\nenable_uart=1\n' > "$CONFIG"
  printf 'console=tty1\n' > "$ROOT/boot/firmware/cmdline.txt"
}

reset_box
dietpi

echo "  -- at boot, no sound card chosen"
boot_with 'CONFIG_SOUNDCARD=none\n'
assert_eq "succeeds" "$STATUS" 0
assert_eq "touches nothing, since DietPi's none would purge ALSA" "$CALLS" ""
boot_with 'AUTO_SETUP_NET_HOSTNAME=kalinka\n'
assert_eq "nor when the key is missing" "$CALLS" ""
assert_no_file "and remembers nothing" "$STATE"

echo "  -- at boot, a HAT named on the card"
boot_with 'CONFIG_SOUNDCARD=hifiberry-digi\n'
assert_eq "succeeds" "$STATUS" 0
assert_contains "applies it through DietPi, unattended" "$CALLS" \
  "dietpi-set_hardware G_INTERACTIVE=0 soundcard hifiberry-digi"
assert_eq "remembers it" "$(cat "$STATE")" hifiberry-digi
assert_contains "writes the change out" "$CALLS" "sync"
assert_contains "restarts to load the overlay" "$CALLS" "systemctl --no-block reboot"

echo "  -- the boot after that restart"
boot_with 'CONFIG_SOUNDCARD=hifiberry-digi\n'
assert_eq "does nothing at all" "$CALLS" ""

echo "  -- a change that needs no new boot configuration"
printf 'dtparam=audio=off\n' > "$CONFIG"
STUB_EDIT=0 boot_with 'CONFIG_SOUNDCARD=usb-dac\n'
assert_contains "applies it" "$CALLS" "soundcard usb-dac"
assert_not_contains "without restarting" "$CALLS" "reboot"
assert_eq "and remembers it" "$(cat "$STATE")" usb-dac

echo "  -- a dietpi.txt saved on Windows"
boot_with 'CONFIG_SOUNDCARD=iqaudio-dacplus\r\n'
assert_contains "reads the name without the carriage return" "$CALLS" "soundcard iqaudio-dacplus"
assert_not_contains "and passes none on" "$CALLS" $'\r'

echo "  -- DietPi failing to apply it"
STUB_FAIL=1 boot_with 'CONFIG_SOUNDCARD=allo-boss-dac-pcm512x-audio\n'
[ "$STATUS" -ne 0 ] || fail "reports the failure"
assert_eq "keeps the last card that worked" "$(cat "$STATE")" iqaudio-dacplus
assert_not_contains "and does not restart into it" "$CALLS" "reboot"

echo "  -- none, then the card it had before"
boot_with 'CONFIG_SOUNDCARD=none\n'
assert_no_file "forgets the card" "$STATE"
boot_with 'CONFIG_SOUNDCARD=iqaudio-dacplus\n'
assert_contains "so naming it again applies it again" "$CALLS" "soundcard iqaudio-dacplus"

echo "  -- at boot, a card DietPi does not know"
reset_box
dietpi
boot_with 'CONFIG_SOUNDCARD=i2s-dac\n'
assert_not_contains "is not handed to DietPi, which would ignore it" "$CALLS" "dietpi-set_hardware"
assert_contains "but set up directly" "$(config)" "dtoverlay=i2s-dac"
assert_contains "and loaded with a restart" "$CALLS" "systemctl --no-block reboot"
STUB_EDIT=0 boot_with 'CONFIG_SOUNDCARD=hw:1,0\n'
assert_contains "while a name only DietPi understands still goes to DietPi" "$CALLS" "soundcard hw:1,0"

echo "  -- a box the image set up before the helper moved"
reset_box
dietpi
mkdir -p "$ROOT/var/lib/kalinka-image"
echo hifiberry-digi > "$ROOT/var/lib/kalinka-image/soundcard"
boot_with 'CONFIG_SOUNDCARD=hifiberry-digi\n'
assert_eq "applies nothing again" "$CALLS" ""
assert_eq "and takes the record over" "$(cat "$STATE")" hifiberry-digi
assert_no_file "from where the image kept it" "$ROOT/var/lib/kalinka-image/soundcard"

echo "  -- the supervisor applying a card on Raspberry Pi OS"
reset_box
no_dietpi
printf 'dtparam=audio=on\n[pi4]\ndtoverlay=vc4-kms-v3d\ndtoverlay=hifiberry-digi\r\n' > "$CONFIG"
run apply hifiberry-dacplus-std
assert_eq "succeeds" "$STATUS" 0
assert_eq "says the boot configuration changed, and nothing else" "$OUT" "changed"
assert_not_contains "never restarts the box itself" "$CALLS" "reboot"
assert_eq "replaces the card, switching the Pi's own audio off, for every board" "$(config)" \
  "$(printf 'dtparam=audio=off\n[pi4]\ndtoverlay=vc4-kms-v3d\n[all]\ndtoverlay=hifiberry-dacplus-std\nforce_eeprom_read=0')"
assert_eq "remembers it" "$(cat "$STATE")" hifiberry-dacplus-std
run apply hifiberry-dacplus-std
assert_eq "applying it again changes nothing" "$OUT" "unchanged"
run apply hifiberry-digi
assert_not_contains "a board whose chip may be read lets the firmware read it" "$(config)" "force_eeprom_read"
assert_eq "and only one card is set up" "$(grep -c '^dtoverlay=hifiberry' "$CONFIG")" 1

echo "  -- the Pi's own outputs, then no HAT at all"
printf 'dtparam=audio=off\ndtoverlay=vc4-kms-v3d,cma-384,noaudio\ndtoverlay=hifiberry-amp3\nforce_eeprom_read=0\n' > "$CONFIG"
run apply rpi-bcm2835-3.5mm
assert_eq "switches onboard audio and HDMI audio back on" "$(config)" \
  "$(printf 'dtparam=audio=on\ndtoverlay=vc4-kms-v3d,cma-384')"
run apply none
assert_eq "no HAT leaves the Pi's own audio off" "$(config)" \
  "$(printf 'dtparam=audio=off\ndtoverlay=vc4-kms-v3d,cma-384')"
assert_no_file "and forgets the card" "$STATE"

echo "  -- the supervisor applying a card on DietPi"
reset_box
dietpi
printf 'CONFIG_SOUNDCARD=none\n' > "$ROOT/boot/dietpi.txt"
run apply i2s-dac
assert_eq "applies a card DietPi does not know" "$OUT" "changed"
assert_not_contains "without DietPi" "$CALLS" "dietpi-set_hardware"
assert_contains "and tells DietPi which card the box has" "$(cat "$ROOT/boot/dietpi.txt")" "CONFIG_SOUNDCARD=i2s-dac"
run apply hifiberry-digi
assert_contains "hands a card DietPi knows to DietPi" "$CALLS" "dietpi-set_hardware G_INTERACTIVE=0 soundcard hifiberry-digi"
assert_eq "keeping DietPi's own output off the verdict" "$OUT" "changed"
assert_not_contains "after removing the card DietPi would have left behind" "$(config)" "i2s-dac"
run apply none
assert_not_contains "never hands none to DietPi, which would purge ALSA" "$CALLS" "dietpi-set_hardware"
assert_contains "but records it" "$(cat "$ROOT/boot/dietpi.txt")" "CONFIG_SOUNDCARD=none"
assert_not_contains "and removes the card" "$(config)" "dtoverlay=hifiberry-digi"

echo "  -- what the helper refuses"
reset_box
no_dietpi
for args in "apply hw:1,0" "apply format-disk" "apply" "apply a b" "" "reboot"; do
  # shellcheck disable=SC2086  # each case is a word list
  run $args
  assert_eq "'$args' is refused as unknown" "$STATUS" 2
done
assert_eq "and nothing was changed" "$(config)" "$(printf 'dtparam=audio=off\nenable_uart=1')"

echo "  -- two changes at once"
mkdir -p "$ROOT/run/lock"
exec 8>"$ROOT/run/lock/kalinka-soundcard.lock"
flock 8
run apply hifiberry-digi
assert_eq "the second waits for nothing and says why" "$STATUS" 75
flock -u 8
exec 8>&-

echo "  -- the boot unit"
unit="$(cat "$UNIT")"
assert_contains "waits for DietPi to know the hardware and import dietpi.txt" \
  "$unit" "After=dietpi-preboot.service dietpi-firstboot.service"
assert_contains "and for the network, which some cards fetch firmware over" "$unit" "After=network-online.target"
assert_contains "pulling it in" "$unit" "Wants=network-online.target"
assert_contains "runs before the renderer opens a sound card" "$unit" "Before=kalinka-renderer.service"
assert_contains "runs the packaged helper" "$unit" "ExecStart=/usr/lib/kalinka-supervisor/soundcard.sh boot"
assert_not_contains "and does not stay active, which would hold off every supervisor action" "$unit" "RemainAfterExit="

exit "$FAILURES"
