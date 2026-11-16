#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

account=kalinka-admin
description='Kalinka dashboard administrator'
# The address the control page was asked from, where the login will most likely come from too.
peer=${1:-}
# The keys-only choice a Raspberry Pi Imager card made for this login; setting a password here is the later request.
card_policy="${KALINKA_SSH_ROOT:-}/etc/ssh/sshd_config.d/kalinka-card-$account.conf"
# The supervisor tells these apart: SSH is on, but the server turns the account away, or takes no password from it.
login_refused=77
passwords_refused=78
IFS= read -r password
[[ ${#password} -ge 12 && ${#password} -le 128 && $password != *[![:print:]]* ]]

# An IPv4 or IPv6 address as its string of bits; fails for anything else.
bits() {
  local addr=$1 out= part i head tail
  local -a parts rest
  if [[ $addr =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    IFS=. read -ra parts <<< "$addr"
    for part in "${parts[@]}"; do
      ((10#$part < 256)) || return 1
      for ((i = 7; i >= 0; i--)); do out+=$(( (10#$part >> i) & 1 )); done
    done
  elif [[ $addr == *:* && $addr =~ ^[0-9a-fA-F:]+$ ]]; then
    if [[ $addr == *::* ]]; then
      head=${addr%%::*} tail=${addr#*::}
      [[ $tail != *::* ]] || return 1
      IFS=: read -ra parts <<< "$head"
      IFS=: read -ra rest <<< "$tail"
      ((${#parts[@]} + ${#rest[@]} < 8)) || return 1
      while ((${#parts[@]} + ${#rest[@]} < 8)); do parts+=(0); done
      parts+=("${rest[@]}")
    else
      IFS=: read -ra parts <<< "$addr"
    fi
    ((${#parts[@]} == 8)) || return 1
    for part in "${parts[@]}"; do
      [[ $part =~ ^[0-9a-fA-F]{1,4}$ ]] || return 1
      for ((i = 15; i >= 0; i--)); do out+=$(( (16#$part >> i) & 1 )); done
    done
  else
    return 1
  fi
  printf '%s\n' "$out"
}

# OpenSSH's match_pattern: * and ? are wildcards, and every other character stands for itself.
matches() {
  local glob= i c
  for ((i = 0; i < ${#2}; i++)); do
    c=${2:i:1}
    case $c in
      [*?]) glob+=$c ;;
      *) glob+="\\$c" ;;
    esac
  done
  [[ $1 == $glob ]]
}

# Whether the peer is in an address or CIDR entry, as OpenSSH's addr_match_list reads one.
in_network() {
  local net len
  net="$(bits "${1%/*}")" || return 1
  len=${#net}
  if [[ $1 == */* ]]; then
    [[ ${1##*/} =~ ^[0-9]{1,3}$ ]] || return 1
    len=$((10#${1##*/}))
    ((len <= ${#net})) || return 1
  fi
  [[ ${#peer_bits} == "${#net}" && ${peer_bits:0:len} == "${net:0:len}" ]]
}

# OpenSSH's match_host_and_ip for the peer, which sshd names by its address unless UseDNS is on.
host_matches() {
  local entry negated matched=
  local -a entries
  IFS=, read -ra entries <<< "$1"
  for entry in "${entries[@]}"; do
    negated=
    if [[ $entry == '!'* ]]; then
      negated=1 entry=${entry:1}
    fi
    if in_network "$entry" || matches "$peer" "${entry,,}"; then
      [[ -z $negated ]] || return 1
      matched=1
    fi
  done
  [[ -n $matched ]]
}

# OpenSSH's match_user for the account: a user pattern, or user@ and a list of hosts.
user_matches() {
  if [[ $1 == *@* ]]; then
    matches "$account" "${1%%@*}" && host_matches "${1#*@}"
  else
    matches "$account" "$1"
  fi
}

# Whether sshd -T's AllowUsers, DenyUsers, AllowGroups and DenyGroups let the account in from the peer.
lets_in() {
  local key value group allow_users= allowed_user= allow_groups= allowed_group=
  while read -r key value; do
    case $key in
      denyusers)
        if user_matches "$value"; then return 1; fi ;;
      allowusers)
        allow_users=1
        if user_matches "$value"; then allowed_user=1; fi ;;
      denygroups)
        for group in "${groups[@]}"; do
          if matches "$group" "$value"; then return 1; fi
        done ;;
      allowgroups)
        allow_groups=1
        for group in "${groups[@]}"; do
          if matches "$group" "$value"; then allowed_group=1; fi
        done ;;
    esac
  done
  [[ (-z $allow_users || -n $allowed_user) && (-z $allow_groups || -n $allowed_group) ]]
}

# Whether sshd -T's settings let a password alone log in; PAM's keyboard-interactive prompt takes one too.
takes_passwords() {
  local key value password=no prompt=no pam=no methods=' any '
  while read -r key value; do
    case $key in
      passwordauthentication) password=$value ;;
      kbdinteractiveauthentication) prompt=$value ;;
      usepam) pam=$value ;;
      authenticationmethods) methods=" $value " ;;
    esac
  done
  [[ $password == yes && ($methods == ' any ' || $methods == *' password '*) ]] ||
    [[ $prompt == yes && $pam == yes && ($methods == ' any ' || $methods == *' keyboard-interactive '* || $methods == *' keyboard-interactive:pam '*) ]]
}

# The peer goes into sshd's -C list, so it must be an address and nothing more.
peer_bits="$(bits "$peer")"

unit=
running=
for candidate in dropbear.service ssh.service sshd.service; do
  state="$(systemctl show --property=LoadState --value "$candidate")"
  [[ $state == loaded ]] || continue
  [[ -n $unit ]] || unit=$candidate
  if systemctl is-active --quiet "$candidate"; then
    unit=$candidate
    running=1
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
  # A server that was stopped has just started without the card's policy.
  [[ -z $reload || -z $running ]] || systemctl reload-or-restart "$unit"
  policy="$(sshd -T -C user="$account",host="$peer",addr="$peer")"
  account_groups="$(id -Gn "$account")"
  read -ra groups <<< "$account_groups"
  lets_in <<< "$policy" || exit "$login_refused"
  takes_passwords <<< "$policy" || exit "$passwords_refused"
fi
