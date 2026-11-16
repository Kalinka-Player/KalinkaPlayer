#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

account=kalinka-admin
description='Kalinka dashboard administrator'
# The keys-only choice a Raspberry Pi Imager card made for this login; setting a password here is the later request.
card_policy="${KALINKA_SSH_ROOT:-}/etc/ssh/sshd_config.d/kalinka-card-$account.conf"
# The supervisor tells this status apart: SSH is on, but the server takes no password for the account.
passwords_refused=78
IFS= read -r password
[[ ${#password} -ge 12 && ${#password} -le 128 && $password != *[![:print:]]* ]]

# Whether sshd -T's settings let a password alone log in; AuthenticationMethods can forbid that too.
takes_passwords() {
  local key value allowed=no methods=any
  while read -r key value; do
    case $key in
      passwordauthentication) allowed=$value ;;
      authenticationmethods) methods=$value ;;
    esac
  done
  [[ $allowed == yes && ($methods == any || " $methods " == *" password "*) ]]
}

unit=
for candidate in dropbear.service ssh.service sshd.service; do
  state="$(systemctl show --property=LoadState --value "$candidate")"
  [[ $state == loaded ]] || continue
  [[ -n $unit ]] || unit=$candidate
  if systemctl is-active --quiet "$candidate"; then
    unit=$candidate
    break
  fi
done
[[ -n $unit ]]
getent group sudo >/dev/null

if entry="$(getent passwd "$account")"; then
  IFS=: read -r name unused uid gid comment home_dir login_shell <<< "$entry"
  [[ $uid -ge 1000 && $comment == "$description" && $home_dir == /home/kalinka-admin && $login_shell == /bin/bash ]]
else
  useradd --create-home --shell /bin/bash --comment "$description" "$account"
fi
usermod --append --groups sudo "$account"
printf '%s:%s\n' "$account" "$password" | chpasswd >/dev/null 2>&1
unset password
reload=
if [[ -e $card_policy ]]; then
  rm -f -- "$card_policy"
  reload=1
fi
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
# Dropbear reads no sshd_config and has no equivalent of -T.
if [[ $unit != dropbear.service ]]; then
  [[ -z $reload ]] || systemctl reload-or-restart "$unit"
  policy="$(sshd -T -C user="$account",host=localhost,addr=127.0.0.1)"
  takes_passwords <<< "$policy" || exit "$passwords_refused"
fi
