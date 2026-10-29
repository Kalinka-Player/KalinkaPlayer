#!/usr/bin/env bash
#
# The steps that take a downloaded DietPi image as trustworthy and keep it
# what DietPi tested: a signature from the pinned key and nobody else, every
# dietpi.txt key the build sets still under its name, and the kernel held
# where DietPi left it.
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

echo "  -- the keys DietPi's dietpi.txt must still have"
settings="$WORK/dietpi.txt"
cat > "$settings" <<'CONF'
# DietPi-Automation settings
AUTO_SETUP_GLOBAL_PASSWORD=dietpi
AUTO_SETUP_AUTOMATED=0
SURVEY_OPTED_IN=-1
AUTO_SETUP_LOGGING_INDEX=-1
AUTO_SETUP_NET_ETHERNET_ENABLED=1
AUTO_SETUP_NET_WIFI_ENABLED=0
AUTO_SETUP_NET_USESTATIC=0
AUTO_SETUP_NET_STATIC_IP=192.168.0.100/24
AUTO_SETUP_NET_STATIC_GATEWAY=192.168.0.1
AUTO_SETUP_NET_STATIC_DNS=9.9.9.9 149.112.112.112
  AUTO_SETUP_NET_HOSTNAME=DietPi
CONFIG_SERIAL_CONSOLE_ENABLE=1
CONF
( require_dietpi_keys "$settings" ) || fail "refused a dietpi.txt with every key the build sets, one indented"
for key in AUTO_SETUP_NET_USESTATIC AUTO_SETUP_NET_STATIC_IP AUTO_SETUP_NET_STATIC_GATEWAY AUTO_SETUP_NET_STATIC_DNS; do
  grep -v "^$key=" "$settings" > "$WORK/without.txt"
  refusal="$( ( require_dietpi_keys "$WORK/without.txt" ) 2>&1 )" && fail "accepted a dietpi.txt without $key"
  assert_eq "names the key it lacks" "${refusal##* }" "$key"
done
sed 's/^AUTO_SETUP_NET_STATIC_IP=/#&/' "$settings" > "$WORK/commented.txt"
( require_dietpi_keys "$WORK/commented.txt" ) 2>/dev/null && fail "took a commented-out key for one DietPi reads"
sed 's/^AUTO_SETUP_NET_STATIC_IP=/AUTO_SETUP_NET_STATIC_IP4=/' "$settings" > "$WORK/renamed.txt"
( require_dietpi_keys "$WORK/renamed.txt" ) 2>/dev/null && fail "took a renamed key that starts with the old name for it"
sed 's|^AUTO_SETUP_NET_STATIC_IP=.*|AUTO_SETUP_NET_STATIC_IP=192.168.0.100\nAUTO_SETUP_NET_STATIC_MASK=255.255.255.0|' "$settings" > "$WORK/netmask.txt"
( require_dietpi_keys "$WORK/netmask.txt" ) 2>/dev/null && fail "took a DietPi whose address has a netmask line, not the prefix the guide gives"
{ cat "$settings"; printf 'aSOFTWARE_INSTALL_STATE[103]=2\n'; } > "$WORK/bracketed.txt"
# shellcheck disable=SC2034  # require_dietpi_keys reads it
( DIETPI_SETTINGS=(["aSOFTWARE_INSTALL_STATE[103]"]=0); require_dietpi_keys "$WORK/bracketed.txt" ) \
  || fail "read a key with brackets as a pattern, not as the name DietPi reads"

echo "  -- the kernel it ships"
in_chroot() {
  printf '%s\n' \
    'linux-image-6.12.57+deb13-amd64 6.12.57-1 install ok installed' \
    'linux-image-6.12.48+deb13-amd64 6.12.48-1 deinstall ok config-files' \
    'linux-image-amd64 6.12.57-1 install ok installed'
}
assert_eq "names each installed kernel package with its version, since a rebuild keeps the name" \
  "$(kernel_packages | tr '\n' ';')" \
  "linux-image-6.12.57+deb13-amd64 6.12.57-1;linux-image-amd64 6.12.57-1;"
kernel_packages > "$WORK/kernel.before"
in_chroot() { echo "$*" >> "$WORK/marked"; }
mark_kernel hold
assert_eq "holds exactly those while the build upgrades" \
  "$(cat "$WORK/marked")" "apt-mark hold linux-image-6.12.57+deb13-amd64 linux-image-amd64"
: > "$WORK/kernel.before"
: > "$WORK/marked"
mark_kernel hold
assert_eq "and asks apt nothing when there is no kernel package" "$(cat "$WORK/marked")" ""
unset -f in_chroot

echo "  -- the signature"
if ! command -v gpg >/dev/null || ! command -v gpgv >/dev/null; then
  echo "    SKIP: needs gpg and gpgv"
  exit "$FAILURES"
fi
export GNUPGHOME="$WORK/gnupg"
mkdir -m 700 "$GNUPGHOME"
new_key() {
  gpg --batch --quiet --passphrase '' --quick-gen-key "$1 <$1@example.com>" ed25519 sign never 2>/dev/null
  gpg --list-keys --with-colons "$1@example.com" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }'
}
pinned="$(new_key pinned)"
other="$(new_key other)"
if [ -z "$pinned" ] || [ -z "$other" ]; then
  fail "could not make the test keys (is gpg-agent installed?)"
  exit "$FAILURES"
fi
gpg --armor --export "$pinned" > "$WORK/pinned.asc"
gpg --armor --export "$pinned" "$other" > "$WORK/both.asc"
echo "an image" > "$WORK/image.xz"
gpg --batch --quiet --local-user "$pinned" --detach-sign --armor -o "$WORK/good.asc" "$WORK/image.xz"
gpg --batch --quiet --local-user "$other" --detach-sign --armor -o "$WORK/other.asc" "$WORK/image.xz"

( verify_signature "$WORK/pinned.asc" "$pinned" "$WORK/image.xz" "$WORK/good.asc" ) \
  || fail "rejected a good signature from the pinned key"
echo "a changed image" > "$WORK/tampered.xz"
( verify_signature "$WORK/pinned.asc" "$pinned" "$WORK/tampered.xz" "$WORK/good.asc" ) 2>/dev/null \
  && fail "accepted a signature over different bytes"
( verify_signature "$WORK/both.asc" "$pinned" "$WORK/image.xz" "$WORK/other.asc" ) 2>/dev/null \
  && fail "accepted a good signature from a key that is not the pinned one"

exit "$FAILURES"
