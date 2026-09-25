#!/usr/bin/env bash
#
# The sound-card hook on the DietPi images: one line in dietpi.txt picks a DAC
# HAT, applied once, with exactly one restart when the boot configuration had
# to change, and never a restart loop.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"

needs_disposable_system

SOUNDCARD="$PKG_DIR/overlays/dietpi/usr/lib/kalinka-image/soundcard.sh"
UNIT="$PKG_DIR/overlays/dietpi/usr/lib/systemd/system/kalinka-soundcard.service"
STATE=/var/lib/kalinka-image/soundcard

# DietPi's dietpi-set_hardware, recording its arguments and, unless told
# otherwise, adding the overlay to config.txt the way the real one does.
make_set_hardware_stub() {
  mkdir -p /boot/dietpi/func
  cat > /boot/dietpi/func/dietpi-set_hardware <<STUB
#!/bin/sh
echo "dietpi-set_hardware G_INTERACTIVE=\$G_INTERACTIVE \$*" >> "$CALL_LOG"
[ "\${STUB_FAIL:-0}" = 1 ] && exit 1
[ "\${STUB_EDIT:-1}" = 1 ] && echo "dtoverlay=\$2" >> /boot/firmware/config.txt
exit 0
STUB
  chmod 755 /boot/dietpi/func/dietpi-set_hardware
}

# Runs the hook over the dietpi.txt on stdin.
run_soundcard() {
  cat > /boot/dietpi.txt
  local saved_path="$PATH"
  make_recorders systemctl
  make_set_hardware_stub
  bash "$SOUNDCARD" >/dev/null 2>&1
  RUN_STATUS=$?
  CALLS="$(calls)"
  PATH="$saved_path"
}

rm -f "$STATE"
mkdir -p /boot/firmware
printf 'dtparam=audio=off\n' > /boot/firmware/config.txt
printf 'console=tty1\n' > /boot/firmware/cmdline.txt

echo "  -- no sound card chosen"
run_soundcard <<< 'CONFIG_SOUNDCARD=none'
assert_eq "succeeds" "$RUN_STATUS" 0
assert_eq "touches nothing, since DietPi's none would purge ALSA" "$CALLS" ""
run_soundcard <<< 'AUTO_SETUP_NET_HOSTNAME=kalinka'
assert_eq "nor when the key is missing" "$CALLS" ""
assert_no_file "and remembers nothing" "$STATE"

echo "  -- a HAT named on the card"
run_soundcard <<< 'CONFIG_SOUNDCARD=hifiberry-digi'
assert_eq "succeeds" "$RUN_STATUS" 0
assert_contains "applies it through DietPi, unattended" "$CALLS" \
  "dietpi-set_hardware G_INTERACTIVE=0 soundcard hifiberry-digi"
assert_eq "remembers it" "$(cat "$STATE")" hifiberry-digi
assert_contains "restarts to load the overlay" "$CALLS" "systemctl --no-block reboot"

echo "  -- the boot after that restart"
run_soundcard <<< 'CONFIG_SOUNDCARD=hifiberry-digi'
assert_eq "does nothing at all" "$CALLS" ""

echo "  -- a change that needs no new boot configuration"
STUB_EDIT=0 run_soundcard <<< 'CONFIG_SOUNDCARD=usb-dac'
assert_contains "applies it" "$CALLS" "soundcard usb-dac"
assert_not_contains "without restarting" "$CALLS" "reboot"
assert_eq "and remembers it" "$(cat "$STATE")" usb-dac

echo "  -- a dietpi.txt saved on Windows"
run_soundcard < <(printf 'CONFIG_SOUNDCARD=iqaudio-dacplus\r\n')
assert_contains "reads the name without the carriage return" "$CALLS" "soundcard iqaudio-dacplus"
assert_not_contains "and passes none on" "$CALLS" $'\r'

echo "  -- DietPi failing to apply it"
STUB_FAIL=1 run_soundcard <<< 'CONFIG_SOUNDCARD=allo-boss-dac-pcm512x-audio'
[ "$RUN_STATUS" -ne 0 ] || fail "reports the failure"
assert_eq "keeps the last card that worked" "$(cat "$STATE")" iqaudio-dacplus
assert_not_contains "and does not restart into it" "$CALLS" "reboot"

echo "  -- none, then the card it had before"
run_soundcard <<< 'CONFIG_SOUNDCARD=none'
assert_no_file "forgets the card" "$STATE"
run_soundcard <<< 'CONFIG_SOUNDCARD=iqaudio-dacplus'
assert_contains "so naming it again applies it again" "$CALLS" "soundcard iqaudio-dacplus"

echo "  -- the unit's place in the boot"
assert_contains "waits for DietPi to know the hardware and import dietpi.txt" \
  "$(cat "$UNIT")" "After=dietpi-preboot.service dietpi-firstboot.service"
assert_contains "and for the network, which some cards fetch firmware over" \
  "$(cat "$UNIT")" "After=network-online.target"
assert_contains "pulling it in" "$(cat "$UNIT")" "Wants=network-online.target"
assert_contains "and runs before the renderer opens a sound card" \
  "$(cat "$UNIT")" "Before=kalinka-renderer.service"

exit "$FAILURES"
