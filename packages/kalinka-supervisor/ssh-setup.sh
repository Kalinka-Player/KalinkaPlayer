#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

account=kalinka-admin
description='Kalinka dashboard administrator'
# The address the control page was asked from, where the login will most likely come from too.
peer=${1:-}
# The keys-only choice a Raspberry Pi Imager card made for this login; setting a password here is the later request.
card_policy="${KALINKA_SSH_ROOT:-}/etc/ssh/sshd_config.d/kalinka-card-$account.conf"
# Setup succeeded, but OpenSSH either refuses password-only authentication or could not be checked.
passwords_refused=78
policy_unknown=79
IFS= read -r password
[[ ${#password} -ge 12 && ${#password} -le 128 && $password != *[![:print:]]* ]]

# Whether sshd -T's settings let a password alone log in; PAM's keyboard-interactive prompt takes one too.
takes_passwords() {
  local key value password= prompt= pam= methods=
  while read -r key value; do
    case ${key,,} in
      passwordauthentication) password=$value ;;
      kbdinteractiveauthentication) prompt=$value ;;
      usepam) pam=$value ;;
      authenticationmethods) methods=$value ;;
    esac
  done
  # Do not turn missing or unfamiliar output into a definite refusal.
  [[ $password =~ ^(yes|no)$ && $prompt =~ ^(yes|no)$ && $pam =~ ^(yes|no)$ && -n $methods ]] || return "$policy_unknown"
  methods=" $methods "
  [[ $password == yes && ($methods == ' any ' || $methods == *' password '*) ]] ||
    [[ $prompt == yes && $pam == yes && ($methods == ' any ' || $methods == *' keyboard-interactive '* || $methods == *' keyboard-interactive:pam '*) ]] || return "$passwords_refused"
}

# The Go caller validates and normalizes the IP address. Guard the -C list against
# extra fields here too, without maintaining another IP parser in shell.
[[ $peer =~ ^[0-9a-fA-F:.]+$ ]]

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
# Only remove the policy owned by the Imager setup, after chpasswd succeeds.
rm -f -- "$card_policy"
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
# Reload every running OpenSSH service, including one alongside Dropbear. Doing
# this on every attempt also recovers if a previous reload failed after removal.
openssh=
for candidate in ssh.service sshd.service; do
  if systemctl is-active --quiet "$candidate"; then
    systemctl reload-or-restart "$candidate"
    openssh=1
  fi
done
# Dropbear reads no sshd_config and has no equivalent of -T. This checks only
# authentication methods; it is not a test login or an access-control evaluator.
if [[ -n $openssh ]]; then
  policy="$(sshd -T -C user="$account",host="$peer",addr="$peer")" || exit "$policy_unknown"
  takes_passwords <<< "$policy" || exit "$?"
fi
