#!/usr/bin/env bash
# Applies CONFIG_SOUNDCARD from dietpi.txt, for DAC HATs the Pi firmware cannot identify.
set -euo pipefail

# shellcheck source=dietpi-conf.sh
. "$(dirname "${BASH_SOURCE[0]}")/dietpi-conf.sh"

STATE=/var/lib/kalinka-image/soundcard

log() { echo "kalinka-soundcard: $*"; }

# A file that is not there is part of the fingerprint too, not a failure.
boot_fingerprint() {
  { cat /boot/firmware/config.txt /boot/firmware/cmdline.txt /etc/modprobe.d/* 2>/dev/null || true; } \
    | sha256sum
}

wanted="$(dietpi_conf_get /boot/dietpi.txt CONFIG_SOUNDCARD)"
# DietPi's "none" also purges alsa-utils, which Kalinka keeps; a card named again later is applied again.
case "$wanted" in
  ''|none) rm -f "$STATE"; exit 0 ;;
esac
[ "$wanted" != "$(cat "$STATE" 2>/dev/null)" ] || exit 0

before="$(boot_fingerprint)"
log "applying the sound card '$wanted'"
G_INTERACTIVE=0 /boot/dietpi/func/dietpi-set_hardware soundcard "$wanted"
mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$wanted" > "$STATE"

if [ "$before" != "$(boot_fingerprint)" ]; then
  log "the boot configuration changed; restarting once to load it"
  systemctl --no-block reboot
fi
