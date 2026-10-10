#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

account=kalinka-admin
description='Kalinka dashboard administrator'
IFS= read -r password
[[ ${#password} -ge 12 && ${#password} -le 128 && $password != *[![:print:]]* ]]

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
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
