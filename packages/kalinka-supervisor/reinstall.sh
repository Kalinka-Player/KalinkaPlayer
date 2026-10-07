#!/usr/bin/env bash
# Run by kalinka-reinstall.service. Fetches the published installer, never a
# script from Core's own installation, which may be what is broken. Downloaded
# to a file, not piped, so a truncated download cannot run half a script.
set -euo pipefail

URL="${KALINKA_INSTALL_SCRIPT_URL:-https://kalinkaplayer.com/install.sh}"

TMP="$(mktemp --suffix=.sh)"
trap 'rm -f "$TMP"' EXIT

echo ">> Fetching installer from $URL"
curl -fsSL "$URL" -o "$TMP"

KALINKA_REINSTALL=1 bash "$TMP"
