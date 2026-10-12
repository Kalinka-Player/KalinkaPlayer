#!/usr/bin/env bash
# Run by kalinka-plugin-removal.service. The package comes from the supervisor's
# request file, which only root writes; nothing on a command line names it.
# Core is stopped for the removal and started once afterwards whether apt
# succeeded or not, so a failed removal never leaves the box silent.
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

IFS= read -r package < "$1"
[[ $package =~ ^kalinka-plugin-[a-z0-9][a-z0-9+.-]*$ && $package != kalinka-plugin-sdk ]]
[[ $(dpkg-query -W -f='${db:Status-Status}' "$package") == installed ]]
# Only the plugin may go: a package the server depends on would take the server with it.
[[ $(apt-get -s remove -y "$package" | awk '/^Remv /{print $2}') == "$package" ]]

echo ">> Removing $package"
systemctl stop kalinka.service
trap 'systemctl reset-failed kalinka.service 2>/dev/null || true; systemctl --no-block restart kalinka.service' EXIT
apt-get -o DPkg::Lock::Timeout=300 remove -y "$package"
[[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]
echo ">> Removed $package"
