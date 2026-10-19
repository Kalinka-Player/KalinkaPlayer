#!/usr/bin/env bash
#
# Run by the root-owned kalinka-upgrade.service when the server requests an
# upgrade. Fetches the CURRENT published installer from the website's stable
# URL (a redirect to the repo's scripts/install-release.sh on main) instead
# of running a deb-shipped copy, so installer fixes apply immediately without
# waiting for a new server release. Downloaded to a temp file, not piped, so
# a truncated download can't execute half a script.
set -euo pipefail

URL="${KALINKA_INSTALL_SCRIPT_URL:-https://kalinkaplayer.com/install.sh}"

# A directory of its own: the installer runs, as root, any installer beside it.
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT
TMP="$DIR/install.sh"

echo ">> Fetching installer from $URL"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP"
else
  wget -qO "$TMP" "$URL"
fi

bash "$TMP"
