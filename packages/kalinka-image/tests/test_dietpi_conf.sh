#!/usr/bin/env bash
#
# dietpi.txt and DietPi's install state, as the build edits them. A setting
# that silently fails to take is how an image ships DietPi's default password,
# so the edits are checked against files shaped like the real ones.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck disable=SC2034  # base-dietpi.sh reads it
SCRIPT_DIR="$PKG_DIR"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/lib/common.sh"
# shellcheck source=/dev/null
. "$PKG_DIR/lib/base-dietpi.sh"

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

echo "  -- the image's DietPi files"
root="$WORK/root"
mkdir -p "$root/boot/firmware" "$root/boot/dietpi" "$root/etc"
cat > "$root/boot/firmware/dietpi.txt" <<'CONF'
AUTO_SETUP_GLOBAL_PASSWORD=dietpi
AUTO_SETUP_NET_HOSTNAME=DietPi
AUTO_SETUP_AUTOMATED=0
SURVEY_OPTED_IN=-1
AUTO_SETUP_LOGGING_INDEX=-1
CONFIG_SOUNDCARD=none
CONF
cp "$root/boot/firmware/dietpi.txt" "$root/boot/dietpi.txt"
printf 'aSOFTWARE_INSTALL_STATE[103]=2\naSOFTWARE_INSTALL_STATE[104]=2\n' > "$root/boot/dietpi/.installed"
cat > "$root/etc/fstab" <<'FSTAB'
tmpfs /tmp tmpfs noatime,lazytime,nodev,nosuid,mode=1777
tmpfs /var/log tmpfs size=50M,noatime,lazytime,nodev,nosuid
UUID=5955f447 / ext4 noatime,lazytime,rw 0 1
FSTAB

configure_dietpi_files "$root"
for copy in "$root/boot/firmware/dietpi.txt" "$root/boot/dietpi.txt"; do
  for key in "${!DIETPI_SETTINGS[@]}"; do
    assert_eq "$key in ${copy#"$root"}" "$(dietpi_conf_get "$copy" "$key")" "${DIETPI_SETTINGS[$key]}"
  done
done
assert_eq "the password is gone, not just emptied elsewhere" \
  "$(grep -c 'dietpi$' "$root/boot/dietpi.txt")" 0
cmp -s "$root/boot/firmware/dietpi.txt" "$root/boot/dietpi.txt" || fail "the two copies differ"
assert_mode "the copy on the root filesystem is private" "$root/boot/dietpi.txt" 600
assert_eq "and older than any edit to the card, so first boot imports the card's" \
  "$(stat -c %Y "$root/boot/dietpi.txt")" 0
assert_eq "DietPi's tools know ALSA is there" \
  "$(dietpi_conf_get "$root/boot/dietpi/.installed" 'aSOFTWARE_INSTALL_STATE[5]')" 2
assert_eq "RAMlog is off" \
  "$(dietpi_conf_get "$root/boot/dietpi/.installed" 'aSOFTWARE_INSTALL_STATE[103]')" 0
assert_eq "and so is its log index" "$(dietpi_conf_get "$root/boot/dietpi/.installed" INDEX_LOGGING)" 0
assert_eq "other software state is kept" \
  "$(dietpi_conf_get "$root/boot/dietpi/.installed" 'aSOFTWARE_INSTALL_STATE[104]')" 2
assert_not_contains "logs no longer live in RAM" "$(cat "$root/etc/fstab")" "/var/log"
assert_contains "while /tmp still does" "$(cat "$root/etc/fstab")" "tmpfs /tmp"
assert_contains "the journal is kept on disk" \
  "$(cat "$root/etc/systemd/journald.conf.d/kalinka-persistent.conf")" "Storage=persistent"
snapshot="$(cat "$root/boot/firmware/dietpi.txt" "$root/boot/dietpi/.installed" "$root/etc/fstab")"
configure_dietpi_files "$root"
assert_eq "running it again changes nothing" \
  "$(cat "$root/boot/firmware/dietpi.txt" "$root/boot/dietpi/.installed" "$root/etc/fstab")" "$snapshot"

exit "$FAILURES"
