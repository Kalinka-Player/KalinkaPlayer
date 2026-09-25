#!/usr/bin/env bash
#
# The first-boot pass. It is the only thing standing between a published image
# and every machine that flashes it sharing one set of SSH host keys, so the
# identity work is checked as closely as the account it creates.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"

needs_disposable_system

FIRSTBOOT="$PKG_DIR/overlays/debootstrap/usr/lib/kalinka-image/firstboot.sh"
BOOT=/boot/efi
CONF="$BOOT/kalinka-firstboot.conf"
STAMP=/var/lib/kalinka-image/firstboot-done

# Runs firstboot.sh with the commands that need a running systemd or
# NetworkManager replaced by recorders.
_run_firstboot() {
  # /etc/issue.d is deliberately not created here: a minimal Debian has no
  # such directory, and firstboot.sh is what has to cope with that.
  rm -rf /etc/issue.d
  mkdir -p "$BOOT" /etc/NetworkManager/system-connections /etc/ssh
  rm -f /etc/modprobe.d/kalinka-regdom.conf
  rm -f /etc/NetworkManager/system-connections/kalinka-wifi.nmconnection
  ln -sfn /usr/share/zoneinfo/Etc/UTC /etc/localtime
  echo Etc/UTC > /etc/timezone
  echo "KALINKA_IMAGE_BOOT=$BOOT" > /etc/default/kalinka-image

  local saved_path="$PATH"
  make_recorders hostnamectl timedatectl systemctl nmcli iw
  [ -z "${FAILING:-}" ] || make_failing_recorders $FAILING
  bash "$FIRSTBOOT" >/dev/null 2>&1
  RUN_STATUS=$?
  CALLS="$(calls)"
  PATH="$saved_path"
}

# ... over the configuration on stdin.
run_firstboot() { cat > "$CONF"; _run_firstboot; }

# ... over a boot partition nobody wrote a configuration to.
run_firstboot_unconfigured() { rm -f "$CONF"; _run_firstboot; }

# Puts the machine back to never having booted this image.
forget_first_boot() { rm -f "$STAMP"; }

echo "  -- with no configuration file"
forget_first_boot
run_firstboot_unconfigured
assert_eq "succeeds anyway" "$RUN_STATUS" 0
assert_file "generates host keys" /etc/ssh/ssh_host_ed25519_key
assert_eq "blanks machine-id for systemd to refill" "$(stat -c %s /etc/machine-id)" 0
assert_contains "says so on the console" \
  "$(cat /etc/issue.d/10-kalinka.issue)" "No login account exists"
assert_file "still marks itself done" "$STAMP"

echo "  -- host keys are this machine's, not the image's"
first_key="$(cat /etc/ssh/ssh_host_ed25519_key)"
forget_first_boot
run_firstboot_unconfigured
assert_not_contains "a second machine gets different keys" \
  "$(cat /etc/ssh/ssh_host_ed25519_key)" "$first_key"

echo "  -- a full configuration"
forget_first_boot
run_firstboot <<'CONFIG'
HOSTNAME=listening-room
USERNAME=dmitry
PASSWORD='hunter2'
SSH_AUTHORIZED_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 test@example'
WIFI_SSID='Studio'
WIFI_PASSWORD='sekrit'
WIFI_COUNTRY=GB
TIMEZONE=Europe/London
CONFIG
assert_eq "succeeds" "$RUN_STATUS" 0
assert_contains "sets the hostname" "$CALLS" "hostnamectl set-hostname listening-room"
assert_contains "points 127.0.1.1 at it" "$(cat /etc/hosts)" "listening-room"
assert_eq "sets the timezone" "$(readlink /etc/localtime)" /usr/share/zoneinfo/Europe/London
assert_eq "and names it for Debian" "$(cat /etc/timezone)" Europe/London
assert_not_contains "without timedated, which refuses it this early" "$CALLS" "timedatectl"

getent passwd dmitry >/dev/null || fail "creates the account"
assert_contains "puts it in sudo" "$(id -nG dmitry)" "sudo"
assert_contains "hashes the password with a modern scheme" \
  "$(getent shadow dmitry | cut -d: -f2)" '$6$'
assert_not_contains "and never stores the plaintext" \
  "$(getent shadow dmitry | cut -d: -f2)" "hunter2"

home="$(getent passwd dmitry | cut -d: -f6)"
assert_contains "installs the ssh key" "$(cat "$home/.ssh/authorized_keys")" "test@example"
assert_mode "keeps authorized_keys private" "$home/.ssh/authorized_keys" 600
assert_mode "and its directory" "$home/.ssh" 700

profile=/etc/NetworkManager/system-connections/kalinka-wifi.nmconnection
assert_contains "writes the wifi profile" "$(cat "$profile")" "ssid=Studio"
assert_contains "with the passphrase" "$(cat "$profile")" "psk=sekrit"
assert_mode "which NetworkManager refuses to read if it is not private" "$profile" 600
assert_contains "persists the regulatory domain" \
  "$(cat /etc/modprobe.d/kalinka-regdom.conf)" "ieee80211_regdom=GB"
assert_contains "and applies it now" "$CALLS" "iw reg set GB"

assert_no_file "takes the secrets off the boot partition" "$CONF"
assert_file "marks itself done" "$STAMP"

echo "  -- a configuration saved on Windows"
run_firstboot < <(printf 'USERNAME=crlf\r\nPASSWORD=x\r\nHOSTNAME=crlf-host\r\n')
assert_eq "succeeds" "$RUN_STATUS" 0
assert_eq "reads the name without the carriage return" "$(getent passwd crlf | cut -d: -f1)" "crlf"
assert_not_contains "and passes no carriage return on" "$CALLS" $'\r'
assert_contains "names the machine" "$CALLS" "hostnamectl set-hostname crlf-host"

echo "  -- a configuration that starts with a byte-order mark"
run_firstboot < <(printf '\xEF\xBB\xBFUSERNAME=bom\nPASSWORD=x\n')
assert_eq "succeeds" "$RUN_STATUS" 0
assert_eq "still reads the first line" "$(getent passwd bom | cut -d: -f1)" "bom"

echo "  -- a setting that cannot be applied"
run_firstboot <<'CONFIG'
USERNAME=badzone
PASSWORD='x'
TIMEZONE=Nowhere/Atlantis
WIFI_SSID='Studio'
WIFI_PASSWORD='sekrit'
CONFIG
assert_eq "reports the failure" "$RUN_STATUS" 1
assert_eq "leaves the time zone alone" "$(readlink /etc/localtime)" /usr/share/zoneinfo/Etc/UTC
assert_eq "still creates the account" "$(getent passwd badzone | cut -d: -f1)" "badzone"
assert_file "still writes the wifi profile" /etc/NetworkManager/system-connections/kalinka-wifi.nmconnection
assert_no_file "and still takes the file away" "$CONF"

echo "  -- a command that fails"
FAILING=hostnamectl run_firstboot <<'CONFIG'
USERNAME=nohostname
PASSWORD='x'
HOSTNAME=unreachable
CONFIG
assert_eq "reports the failure" "$RUN_STATUS" 1
assert_eq "still creates the account" "$(getent passwd nohostname | cut -d: -f1)" "nohostname"
assert_no_file "and still takes the file away" "$CONF"

echo "  -- a hash instead of a plaintext password"
run_firstboot <<'CONFIG'
USERNAME=hashed
PASSWORD_HASH='$6$abcdefgh$0123456789'
CONFIG
assert_eq "uses the hash as given" "$(getent shadow hashed | cut -d: -f2)" '$6$abcdefgh$0123456789'

echo "  -- turning ssh off"
run_firstboot <<'CONFIG'
USERNAME=nossh
PASSWORD='x'
SSH_ENABLE=0
CONFIG
assert_contains "disables both units" "$CALLS" "systemctl disable --now ssh.socket ssh.service"

echo "  -- ssh is left alone otherwise"
run_firstboot <<'CONFIG'
USERNAME=withssh
PASSWORD='x'
CONFIG
assert_not_contains "nothing touches ssh" "$CALLS" "ssh.socket"

echo "  -- an account with no way to log in"
run_firstboot <<'CONFIG'
USERNAME=locked
CONFIG
assert_eq "is created anyway" "$(getent passwd locked | cut -d: -f1)" "locked"
assert_contains "and locked" "$(getent shadow locked | cut -d: -f2)" "!"

echo "  -- the console notice follows whether anyone can log in"
run_firstboot_unconfigured
assert_not_contains "stops saying there is no account once there is one" \
  "$(cat /etc/issue.d/10-kalinka.issue)" "No login account exists"

echo "  -- a configuration dropped on a machine already in service"
before_key="$(cat /etc/ssh/ssh_host_ed25519_key)"
run_firstboot <<'CONFIG'
USERNAME=later
PASSWORD='x'
HOSTNAME=renamed
CONFIG
assert_eq "succeeds" "$RUN_STATUS" 0
assert_eq "keeps the host keys this machine already published" \
  "$(cat /etc/ssh/ssh_host_ed25519_key)" "$before_key"
assert_eq "still creates the account" "$(getent passwd later | cut -d: -f1)" "later"
assert_contains "still renames the machine" "$CALLS" "hostnamectl set-hostname renamed"
assert_no_file "and still takes the file away" "$CONF"

exit "$FAILURES"
