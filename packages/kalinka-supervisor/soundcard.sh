#!/usr/bin/env bash
# Sets a Raspberry Pi up for one sound card: the overlay the firmware loads,
# through DietPi's own tool for the cards it knows, and directly otherwise.
#
#   soundcard.sh boot           Apply CONFIG_SOUNDCARD from DietPi's dietpi.txt
#                               once, restarting if the boot configuration changed.
#   soundcard.sh apply <name> [<hat-card>]
#                               Apply <name>, then print "changed" or "unchanged".
#                               <hat-card> is the card the firmware sets up from the
#                               HAT's ID chip, if any. Never restarts: the supervisor
#                               does, after replying.
#
# Exit status: 0 done, 1 failed, 2 unknown name or usage, 75 another run holds the lock.
set -euo pipefail

ROOT="${KALINKA_SOUNDCARD_ROOT:-}"
CONFIG="$ROOT/boot/firmware/config.txt"
DIETPI_TXT="$ROOT/boot/dietpi.txt"
SET_HARDWARE="$ROOT/boot/dietpi/func/dietpi-set_hardware"
STATE="$ROOT/var/lib/kalinka-supervisor/soundcard"
IMAGE_STATE="$ROOT/var/lib/kalinka-image/soundcard"
LOCK="$ROOT/run/lock/kalinka-soundcard.lock"
NAME='^[a-z0-9][a-z0-9.-]{0,47}$'

# Every overlay this helper may have written, so a new choice replaces the old one.
AUDIO_OVERLAYS='hifiberry-[a-z0-9-]+|iqaudio-[a-z0-9-]+|rpi-dacplus|rpi-dacpro|rpi-digiampplus|rpi-codeczero|allo-[a-z0-9-]+|justboom-[a-z0-9-]+|i-sabre-q2m|applepi-dac|dionaudio-[a-z0-9-]+|i2s-dac|i2s-master-dac'

export G_INTERACTIVE=0

# shellcheck source=dietpi-conf.sh
. "$(dirname "${BASH_SOURCE[0]}")/dietpi-conf.sh"

log() { echo "kalinka-soundcard: $*" >&2; }

usage() {
  echo "usage: $0 boot | apply <name> [<hat-card>]" >&2
  exit 2
}

# The cards DietPi's soundcard tool sets up itself; it ignores any other name.
dietpi_handles() {
  case "$1" in
    hifiberry-*|justboom-*|allo-*|rpi-*|i-sabre-q2m|applepi*|iqaudio-*|dionaudio-loco*|usb-dac) return 0 ;;
  esac
  return 1
}

native_handles() {
  [[ "$1" =~ ^($AUDIO_OVERLAYS)$ ]] && return 0
  case "$1" in
    rpi-bcm2835-*|usb-dac|none) return 0 ;;
  esac
  return 1
}

# HiFiBerry's own setup tool stops the firmware reading these boards' ID
# chips, whose overlay older firmware would otherwise load on top of the configured one.
eeprom_off() {
  case "$1" in
    hifiberry-amp100|hifiberry-amp3|hifiberry-dacplushd|hifiberry-digi-pro|hifiberry-dacplus-std) return 0 ;;
  esac
  return 1
}

# A file that is not there is part of the fingerprint too, not a failure.
boot_fingerprint() {
  { cat "$CONFIG" "$ROOT/boot/firmware/cmdline.txt" "$ROOT"/etc/modprobe.d/* 2>/dev/null || true; } \
    | sha256sum
}

# Appends a line where every board reads it: after the last section, if that was for some boards only.
append_line() {
  local last
  last="$(grep -E '^[[:blank:]]*\[' "$CONFIG" | tail -n1 | tr -d '[:blank:]\r' || true)"
  if [ -s "$CONFIG" ] && [ -n "$(tail -c1 "$CONFIG")" ]; then
    printf '\n' >> "$CONFIG"
  fi
  if [ -n "$last" ] && [ "$last" != '[all]' ]; then
    printf '[all]\n' >> "$CONFIG"
  fi
  printf '%s\n' "$1" >> "$CONFIG"
}

remove_audio_overlays() {
  sed -Ei "/^[[:blank:]]*dtoverlay=($AUDIO_OVERLAYS)([,[:blank:]].*)?\r?$/d" "$CONFIG"
}

set_onboard_audio() {
  if grep -Eq '^[[:blank:]]*dtparam=audio=(on|off)[[:blank:]]*\r?$' "$CONFIG"; then
    sed -Ei "s/^([[:blank:]]*dtparam=audio=)(on|off)([[:blank:]]*\r?)$/\1$1\3/" "$CONFIG"
  else
    append_line "dtparam=audio=$1"
  fi
}

# Stops the firmware loading the card a HAT's ID chip names when another is chosen.
set_eeprom_read() {
  local name="$1" hat="$2"
  sed -Ei '/^[[:blank:]]*force_eeprom_read=0[[:blank:]]*\r?$/d' "$CONFIG"
  if { [ -n "$hat" ] && [ "$name" != "$hat" ]; } || { [ -z "$hat" ] && eeprom_off "$name"; }; then
    append_line force_eeprom_read=0
  fi
}

record_choice() {
  if [ -f "$DIETPI_TXT" ]; then
    dietpi_conf_set "$DIETPI_TXT" CONFIG_SOUNDCARD "$1"
  fi
}

apply_native() {
  remove_audio_overlays
  case "$1" in
    rpi-bcm2835-*)
      set_onboard_audio on
      # KMS keeps HDMI audio off with this parameter; the Pi's own output needs it back.
      sed -Ei 's/^([[:blank:]]*dtoverlay=vc4-f?kms-v3d[^[:blank:]]*),noaudio/\1/' "$CONFIG"
      ;;
    usb-dac|none)
      set_onboard_audio off
      ;;
    *)
      set_onboard_audio off
      append_line "dtoverlay=$1"
      ;;
  esac
}

# Applies a card, by whichever way knows it; anything else is DietPi's to interpret.
apply_card() {
  local name="$1" hat="${2:-}"
  if [ "$name" = "$hat" ]; then
    # The firmware sets this card up from the chip; any overlay written for it would load a second copy.
    remove_audio_overlays
    record_choice "$name"
  elif [ "$name" != none ] && [ -x "$SET_HARDWARE" ] && { dietpi_handles "$name" || ! native_handles "$name"; }; then
    # What DietPi does not know of, such as i2s-dac, its reset leaves behind.
    remove_audio_overlays
    "$SET_HARDWARE" soundcard "$name" >&2
  else
    native_handles "$name" || { log "no way to set up '$name'"; return 2; }
    apply_native "$name"
    record_choice "$name"
  fi
  set_eeprom_read "$name" "$hat"
  mkdir -p "$(dirname "$STATE")"
  if [ "$name" = none ]; then
    rm -f "$STATE"
  else
    printf '%s\n' "$name" > "$STATE"
  fi
}

lock() {
  mkdir -p "$(dirname "$LOCK")"
  exec 9>"$LOCK"
  flock -n 9 || { log "another sound card change is under way"; exit 75; }
}

# Boxes set up before the supervisor shipped this helper kept their state with the image.
adopt_image_state() {
  if [ -f "$IMAGE_STATE" ] && [ ! -e "$STATE" ]; then
    mkdir -p "$(dirname "$STATE")"
    mv "$IMAGE_STATE" "$STATE"
  fi
}

boot() {
  local wanted before
  # DietPi on a PC names sound cards too, but only a Pi has a card to set up here.
  [ -f "$CONFIG" ] || return 0
  wanted="$(dietpi_conf_get "$DIETPI_TXT" CONFIG_SOUNDCARD)"
  # DietPi's "none" also purges alsa-utils, which Kalinka keeps; a card named again later is applied again.
  case "$wanted" in
    ''|none) rm -f "$STATE"; return 0 ;;
  esac
  [ "$wanted" != "$(cat "$STATE" 2>/dev/null)" ] || return 0
  before="$(boot_fingerprint)"
  log "applying the sound card '$wanted'"
  apply_card "$wanted"
  if [ "$before" != "$(boot_fingerprint)" ]; then
    log "the boot configuration changed; restarting once to load it"
    sync
    systemctl --no-block reboot
  fi
}

apply() {
  local name="$1" hat="$2" before
  [[ "$name" =~ $NAME ]] || usage
  [ -z "$hat" ] || [[ "$hat" =~ $NAME ]] || usage
  # Only the verdict goes to standard output; what the tools print is diagnostics.
  exec 3>&1 1>&2
  before="$(boot_fingerprint)"
  log "applying the sound card '$name'"
  apply_card "$name" "$hat"
  if [ "$before" != "$(boot_fingerprint)" ]; then
    sync
    echo changed >&3
  else
    echo unchanged >&3
  fi
}

case "${1:-}" in
  boot)
    [ $# -eq 1 ] || usage
    lock
    adopt_image_state
    boot
    ;;
  apply)
    [ $# -eq 2 ] || [ $# -eq 3 ] || usage
    lock
    adopt_image_state
    apply "$2" "${3:-}"
    ;;
  *) usage ;;
esac
