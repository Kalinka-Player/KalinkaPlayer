#!/usr/bin/env bash
#
# Apply the operator's configuration from the boot partition.
#
# Two jobs with different lifetimes, which is why the unit carries no
# condition and the decision lives here. Giving the machine an identity of its
# own — host keys, machine-id — happens once, and is what keeps a published
# image from handing every machine that flashes it the same secrets. Applying
# a configuration file happens whenever one is there, so the same file put
# back on a machine already in service is a way in when there is no shell.
set -euo pipefail

CONF_NAME=kalinka-firstboot.conf
STAMP=/var/lib/kalinka-image/firstboot-done
ISSUE=/etc/issue.d/10-kalinka.issue

# shellcheck source=/dev/null
. /etc/default/kalinka-image
CONF="$KALINKA_IMAGE_BOOT/$CONF_NAME"

FAILED=0

log() { echo "kalinka-firstboot: $*"; }

# set -e is off inside this ||, so each step returns its own failures.
attempt() {
  "$@" || { log "$1 failed; carrying on with the rest"; FAILED=1; }
}

take_own_identity() {
  rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
  ssh-keygen -A
  : > /etc/machine-id
  mkdir -p "$(dirname "$STAMP")"
  : > "$STAMP"
}

set_hostname() {
  local name="$1"
  hostnamectl set-hostname "$name" || return
  # Rewritten in place rather than replaced: /etc/hosts is a bind mount often
  # enough that swapping the inode under it is not worth the tidier code.
  local hosts
  hosts="$(sed -E "s/^(127\.0\.1\.1[[:space:]]+).*/\1$name/" /etc/hosts)" || return
  printf '%s\n' "$hosts" > /etc/hosts || return
  grep -q '^127\.0\.1\.1' /etc/hosts || echo "127.0.1.1	$name" >> /etc/hosts
}

# timedated refuses this during early boot, so the link is made directly.
set_timezone() {
  local zone="$1"
  case "$zone" in
    /*|*..*) log "unknown time zone '$zone'"; return 1 ;;
  esac
  [ -f "/usr/share/zoneinfo/$zone" ] || { log "unknown time zone '$zone'"; return 1; }
  ln -sfn "/usr/share/zoneinfo/$zone" /etc/localtime || return
  printf '%s\n' "$zone" > /etc/timezone
}

create_account() {
  local name="$1" hash="$2" key="$3"
  id -u "$name" >/dev/null 2>&1 || useradd -m -s /bin/bash "$name" || return
  usermod -aG sudo "$name" || return
  if [ -n "$hash" ]; then
    usermod -p "$hash" "$name" || return
  else
    passwd -l "$name" >/dev/null || return
  fi
  if [ -n "$key" ]; then
    local home
    home="$(getent passwd "$name" | cut -d: -f6)" || return
    install -d -m 700 -o "$name" -g "$name" "$home/.ssh" || return
    printf '%s\n' "$key" > "$home/.ssh/authorized_keys" || return
    chown "$name:$name" "$home/.ssh/authorized_keys" || return
    chmod 600 "$home/.ssh/authorized_keys"
  fi
}

set_up_account() {
  local hash="$PASSWORD_HASH"
  if [ -z "$hash" ] && [ -n "$PASSWORD" ]; then
    hash="$(openssl passwd -6 "$PASSWORD")" || return
  fi
  create_account "$USERNAME" "$hash" "$SSH_AUTHORIZED_KEY" || return
  if [ -z "$hash" ] && [ -z "$SSH_AUTHORIZED_KEY" ]; then
    log "$USERNAME has neither a password nor a key and cannot log in"
  else
    log "created $USERNAME"
  fi
}

# Nothing enables ssh: openssh-server arrives enabled, and only the refusal is ours.
disable_ssh() {
  systemctl disable --now ssh.socket ssh.service || true
  log "ssh disabled"
}

configure_wifi() {
  local ssid="$1" psk="$2" country="$3"
  if [ -n "$country" ]; then
    mkdir -p /etc/modprobe.d || return
    echo "options cfg80211 ieee80211_regdom=$country" > /etc/modprobe.d/kalinka-regdom.conf || return
    iw reg set "$country" || log "could not apply the $country regulatory domain now; it takes effect on reboot"
  fi
  # NetworkManager ignores a profile any other user could read.
  local profile=/etc/NetworkManager/system-connections/kalinka-wifi.nmconnection
  install -D -m 600 /dev/null "$profile" || return
  cat > "$profile" <<PROFILE || return
[connection]
id=kalinka-wifi
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=$ssid

[wifi-security]
key-mgmt=wpa-psk
psk=$psk

[ipv4]
method=auto

[ipv6]
method=auto
PROFILE
  nmcli connection reload || return
  nmcli connection up kalinka-wifi || log "Wi-Fi did not associate yet; NetworkManager will keep retrying"
}

# FAT keeps no permissions, so the secrets this file carried must not outlive the run.
forget_configuration() {
  [ -e "$CONF" ] || return 0
  shred -u "$CONF" 2>/dev/null || rm -f "$CONF"
}

# Windows editors add CRLF line ends and sometimes a byte-order mark; neither may reach a value.
read_configuration() {
  # shellcheck source=/dev/null
  . <(sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$CONF")
}

apply_configuration() {
  HOSTNAME=kalinka
  USERNAME=
  PASSWORD=
  PASSWORD_HASH=
  SSH_AUTHORIZED_KEY=
  SSH_ENABLE=1
  WIFI_SSID=
  WIFI_PASSWORD=
  WIFI_COUNTRY=
  TIMEZONE=

  trap forget_configuration EXIT
  attempt read_configuration
  attempt set_hostname "$HOSTNAME"
  [ -z "$TIMEZONE" ] || attempt set_timezone "$TIMEZONE"
  [ -z "$USERNAME" ] || attempt set_up_account
  [ "$SSH_ENABLE" = 1 ] || attempt disable_ssh
  [ -z "$WIFI_SSID" ] || attempt configure_wifi "$WIFI_SSID" "$WIFI_PASSWORD" "$WIFI_COUNTRY"
  forget_configuration
}

# \4 is agetty's escape for this machine's address, filled in per console.
announce_status() {
  mkdir -p "$(dirname "$ISSUE")"
  if getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 { found = 1 } END { exit !found }'; then
    cat > "$ISSUE" <<NOTICE
Kalinka Player is running: open http://\4:8000 in a browser.

NOTICE
  else
    cat > "$ISSUE" <<NOTICE
Kalinka Player is running: open http://\4:8000 in a browser.
No login account exists on this machine. To create one, write $CONF_NAME to
the boot partition (see the example file beside it) and reboot.

NOTICE
  fi
}

[ -e "$STAMP" ] || take_own_identity
if [ -r "$CONF" ]; then
  apply_configuration
else
  log "no $CONF_NAME on the boot partition — nothing to apply"
fi
announce_status
exit "$FAILED"
