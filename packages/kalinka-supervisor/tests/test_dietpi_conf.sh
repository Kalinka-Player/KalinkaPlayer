#!/usr/bin/env bash
#
# Reading and writing dietpi.txt the way DietPi does, for the helpers that
# take settings from it on a DietPi streamer.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=../../kalinka-image/tests/helpers.sh
. "$PKG_DIR/../kalinka-image/tests/helpers.sh"
# shellcheck source=../dietpi-conf.sh
. "$PKG_DIR/dietpi-conf.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
conf="$WORK/dietpi.txt"

cat > "$conf" <<'CONF'
# DietPi-Automation settings
AUTO_SETUP_GLOBAL_PASSWORD=dietpi
  AUTO_SETUP_NET_HOSTNAME=DietPi
#AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 AAAA mySSHkey
AUTO_SETUP_CUSTOM_SCRIPT_EXEC=https://example.com/a=b
aSOFTWARE_INSTALL_STATE[103]=2
CONFIG_SOUNDCARD=none
CONFIG_SOUNDCARD=second
CONF

echo "  -- reading"
assert_eq "a plain key" "$(dietpi_conf_get "$conf" AUTO_SETUP_GLOBAL_PASSWORD)" dietpi
assert_eq "one indented as DietPi allows" "$(dietpi_conf_get "$conf" AUTO_SETUP_NET_HOSTNAME)" DietPi
assert_eq "a commented-out key is not set" "$(dietpi_conf_get "$conf" AUTO_SETUP_SSH_PUBKEY)" ""
assert_eq "a value that holds an =" \
  "$(dietpi_conf_get "$conf" AUTO_SETUP_CUSTOM_SCRIPT_EXEC)" "https://example.com/a=b"
assert_eq "a key with brackets, taken literally" \
  "$(dietpi_conf_get "$conf" 'aSOFTWARE_INSTALL_STATE[103]')" 2
assert_eq "the first of two wins" "$(dietpi_conf_get "$conf" CONFIG_SOUNDCARD)" none
assert_eq "a missing key is empty" "$(dietpi_conf_get "$conf" NOT_THERE)" ""
printf 'CONFIG_SOUNDCARD=hifiberry-digi\r\n' > "$WORK/crlf.txt"
assert_eq "a file saved on Windows" "$(dietpi_conf_get "$WORK/crlf.txt" CONFIG_SOUNDCARD)" hifiberry-digi

echo "  -- writing"
chmod 640 "$conf"
before="$(cat "$conf")"
dietpi_conf_set "$conf" AUTO_SETUP_GLOBAL_PASSWORD ""
assert_eq "empties a value" "$(dietpi_conf_get "$conf" AUTO_SETUP_GLOBAL_PASSWORD)" ""
assert_eq "changes only that line" \
  "$(diff <(echo "$before") "$conf" | grep -c '^[<>]')" 2
assert_mode "keeps the file's mode" "$conf" 640
dietpi_conf_set "$conf" AUTO_SETUP_SSH_PUBKEY 'ssh-ed25519 AAAA me'
assert_contains "leaves the commented-out line alone" "$(cat "$conf")" "#AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 AAAA mySSHkey"
assert_eq "and appends the key it lacked" "$(tail -1 "$conf")" "AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 AAAA me"
dietpi_conf_set "$conf" 'aSOFTWARE_INSTALL_STATE[103]' 0
assert_eq "sets a bracketed key" "$(dietpi_conf_get "$conf" 'aSOFTWARE_INSTALL_STATE[103]')" 0
dietpi_conf_set "$conf" AUTO_SETUP_NET_HOSTNAME 'a\b&c/d$e'
assert_eq "writes a value with characters sed and awk would read" \
  "$(dietpi_conf_get "$conf" AUTO_SETUP_NET_HOSTNAME)" 'a\b&c/d$e'
once="$(cat "$conf")"
dietpi_conf_set "$conf" AUTO_SETUP_NET_HOSTNAME 'a\b&c/d$e'
assert_eq "writing the same value twice changes nothing" "$(cat "$conf")" "$once"

exit "$FAILURES"
