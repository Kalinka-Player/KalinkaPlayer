#!/usr/bin/env bash
#
# The units that apply Imager's settings and the build steps that put them on
# both bases: when they run and in what order, that systemd.run= runs nothing,
# and that the build fails without any one of the pieces.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common card-settings; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

UNITS="$PKG_DIR/overlays/debootstrap/usr/lib/systemd/system"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "  -- the settings unit"
settings="$(cat "$UNITS/kalinka-card-settings.service")"
before=" $(sed -n 's/^Before=//p' "$UNITS/kalinka-card-settings.service") "
for unit in network-pre.target NetworkManager.service avahi-daemon.service bluetooth.service \
    ssh.service kalinka-supervisor.service kalinka-card-network-wait.service; do
  assert_contains "runs before $unit" "$before" " $unit "
done
for boot in /boot/firmware /boot/efi; do
  for file in custom.toml firstrun.sh user-data network-config; do
    assert_contains "runs for $boot/$file" "$settings" "ConditionPathExists=|$boot/$file"
  done
done
assert_contains "and for Imager's command line alone" "$settings" $'\nConditionKernelCommandLine=|systemd.run\n'
# One condition without the | would have to hold as well, and stop the unit on most cards.
assert_eq "every condition is one that triggers it" \
  "$(grep -c '^Condition[A-Za-z]*=[^|]' <<<"$settings")" 0
assert_not_contains "after the boot partition is mounted" "$settings" "DefaultDependencies=no"
assert_contains "under the image's own Python, isolated" "$settings" \
  "ExecStart=/usr/bin/python3 -I $CARD_SETTINGS_PROGRAM"$'\n'
assert_contains "for a bounded time" "$settings" $'\nTimeoutStartSec=2min\n'
assert_contains "in every start" "$settings" "WantedBy=multi-user.target"

echo "  -- the wait for the card's network"
wait="$(cat "$UNITS/kalinka-card-network-wait.service")"
assert_contains "only when the card gave a network" "$wait" \
  "ConditionPathExists=/run/kalinka-image/card-network"
assert_contains "after NetworkManager has the profile" "$wait" \
  "After=NetworkManager.service kalinka-card-settings.service"
assert_contains "before the supervisor can take the radio" "$wait" "Before=kalinka-supervisor.service"
assert_contains "for a bounded time" "$wait" "ExecStart=/usr/bin/nm-online -s -q --timeout=90"
assert_contains "and timing out is no failure" "$wait" "SuccessExitStatus=1"
assert_contains "in every start" "$wait" "WantedBy=multi-user.target"

echo "  -- the target Imager's command line names"
target="$(cat "$UNITS/kernel-command-line.target")"
assert_contains "starts the normal boot" "$target" $'\nRequires=default.target\n'
assert_contains "after it" "$target" $'\nAfter=default.target\n'

echo "  -- both bases install and check it"
hook="$(cat "$PKG_DIR/rpi-image-gen/kalinka-hook.sh")"
[[ "$hook" == *'. "$SCRIPT_DIR/lib/card-settings.sh"'* ]] || fail "the Pi hook does not load lib/card-settings.sh"
[[ "$hook" == *$'\ninstall_card_settings\nverify_card_settings\nseal_kalinka'* ]] \
  || fail "the Pi images do not get the card settings"
base="$(cat "$PKG_DIR/lib/base-debootstrap.sh")"
[[ "$base" == *$'  install_overlay debootstrap\n  install_card_settings\n'* ]] \
  || fail "the PC image does not get the card settings"
[[ "$base" == *$'\n  verify_card_settings\n}'* ]] || fail "the PC image does not check the card settings"
grep -q '^for lib in .* card-settings .*; do$' "$PKG_DIR/build-image.sh" \
  || fail "build-image.sh does not load lib/card-settings.sh"

# The chroot, as far as these steps reach into it: enabling units, and running a program there.
in_chroot() {
  case "$1" in
    systemctl)
      [ "$2" = enable ] || return 1
      shift 2
      mkdir -p "$ROOTFS/etc/systemd/system/multi-user.target.wants"
      local unit
      for unit in "$@"; do
        ln -sfn "/usr/lib/systemd/system/$unit" "$ROOTFS/etc/systemd/system/multi-user.target.wants/$unit"
      done ;;
    python3) python3 "$2" "$ROOTFS$3" "${@:4}" ;;
    systemd-analyze) printf '%s\n' "$*" > "$WORK/analyzed"; "$ROOTFS/usr/bin/systemd-analyze" ;;
    *) return 1 ;;
  esac
}

# Fails the test unless verify_card_settings refuses the root, naming why.
refuses() {
  local message
  if message="$(verify_card_settings 2>&1)"; then
    fail "took a root $1"
  else
    assert_contains "refuses a root $1" "$message" "$2"
  fi
}

echo "  -- installing it into a root"
SCRIPT_DIR="$PKG_DIR"
ROOTFS="$WORK/root"
tools=(/usr/bin/nm-online /usr/sbin/useradd /usr/sbin/usermod /usr/sbin/chpasswd /usr/sbin/iw)
mkdir -p "$ROOTFS/usr/bin" "$ROOTFS/usr/sbin"
for tool in "${tools[@]}"; do
  printf '#!/bin/sh\n' > "$ROOTFS$tool"
  chmod 755 "$ROOTFS$tool"
done
install_card_settings
for file in "${CARD_SETTINGS_FILES[@]}"; do
  cmp -s "$PKG_DIR/overlays/debootstrap$file" "$ROOTFS$file" || fail "$file is not the overlay's"
  assert_mode "$file is installed read-only" "$ROOTFS$file" 644
done
assert_eq "systemd.run= runs nothing" "$(readlink "$ROOTFS$SYSTEMD_RUN_GENERATOR")" /dev/null
message="$(verify_card_settings 2>&1)" || fail "refused a root with every piece: $message"

echo "  -- systemd's own check, where the image has it"
printf '#!/bin/sh\nexit 0\n' > "$ROOTFS/usr/bin/systemd-analyze"
chmod 755 "$ROOTFS/usr/bin/systemd-analyze"
message="$(verify_card_settings 2>&1)" || fail "refused units systemd-analyze took: $message"
assert_eq "it checks the units alone" "$(cat "$WORK/analyzed")" \
  "systemd-analyze verify --recursive-errors=no --man=no ${CARD_SETTINGS_FILES[*]:1}"
printf '#!/bin/sh\nexit 1\n' > "$ROOTFS/usr/bin/systemd-analyze"
refuses "whose units systemd-analyze finds fault with" "systemd finds fault with the card settings units"
rm "$ROOTFS/usr/bin/systemd-analyze"

echo "  -- a root missing a piece"
rm "$ROOTFS/etc/systemd/system/multi-user.target.wants/kalinka-card-network-wait.service"
refuses "where the wait is not enabled" "kalinka-card-network-wait.service is not enabled in multi-user.target"
install_card_settings

rm "$ROOTFS$SYSTEMD_RUN_GENERATOR"
: > "$ROOTFS$SYSTEMD_RUN_GENERATOR"
refuses "whose systemd.run= generator is not masked" "would still run Imager's script"
rm "$ROOTFS$SYSTEMD_RUN_GENERATOR"
install_card_settings

mv "$ROOTFS/usr/lib/systemd/system/kernel-command-line.target" "$WORK/target"
refuses "without Imager's target" "would start in rescue.target"
mv "$WORK/target" "$ROOTFS/usr/lib/systemd/system/kernel-command-line.target"

for tool in "${tools[@]}"; do
  mv "$ROOTFS$tool" "$WORK/tool"
  refuses "without $tool" "$tool is missing"
  mv "$WORK/tool" "$ROOTFS$tool"
done

printf 'import no_such_module\n' > "$ROOTFS$CARD_SETTINGS_PROGRAM"
refuses "whose Python cannot run the program" "the card settings program does not run"
install_card_settings
message="$(verify_card_settings 2>&1)" || fail "refused the root once mended: $message"

exit "$FAILURES"
