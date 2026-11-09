#!/usr/bin/env bash
#
# Inside a throwaway Debian 13 container: make it the system named, as its
# owner has it when following docs/installation.md, then run that system's
# install cases. run.sh starts one container per system.
#
# Usage: system.sh debian|dietpi
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
system="${1:-}"

case "$system" in
  debian|dietpi) ;;
  *) echo 'usage: system.sh debian|dietpi' >&2; exit 2 ;;
esac

# No init runs here: maintainer scripts start nothing, and what they ask systemctl is only noted.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
# Diverted, as apt runs maintainer scripts on a PATH of its own and systemd, which bluez brings, ships one.
dpkg-divert --local --rename --divert /usr/bin/systemctl.real --add /usr/bin/systemctl
install -m 755 "$here/systemctl-recorder" /usr/bin/systemctl

if [ "$system" = dietpi ]; then
  printf '%s\n' 'APT::Install-Recommends "false";' 'APT::Install-Suggests "false";' \
    'APT::AutoRemove::RecommendsImportant "false";' 'APT::AutoRemove::SuggestsImportant "false";' \
    > /etc/apt/apt.conf.d/97dietpi
fi

apt-get update -q
# DietPi's base system carries curl and the CA certificates; without recommends, curl alone brings no certificates.
[ "$system" != dietpi ] || apt-get install -y -q ca-certificates curl
# What the guide has its reader install before the install command.
apt-get install -y -q ca-certificates curl python3

exec python3 -u "$here/cases.py" "$system"
