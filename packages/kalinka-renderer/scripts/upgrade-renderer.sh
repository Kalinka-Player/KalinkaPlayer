#!/usr/bin/env bash
#
# Run by the root-owned kalinka-renderer-upgrade.service when the renderer
# requests an upgrade. The renderer runs unprivileged and can only ask: it
# writes the version it was told to install into the trigger file, and this
# installs it.
#
# The installer is fetched from the repo rather than run from a package-shipped
# copy, so installer fixes reach a box without waiting for a renderer release —
# the same reasoning as the server's upgrade.sh. Downloaded to a temp file, not
# piped, so a truncated download cannot execute half a script.
set -euo pipefail

# The website's stable short URL, a redirect to the repo's install-renderer.sh
# on main — the same indirection the server's upgrade.sh uses, so the script
# stays current and the URL survives the repo moving.
URL="${KALINKA_RENDERER_INSTALLER:-https://kalinkaplayer.com/install-renderer.sh}"
TRIGGER="${KALINKA_RENDERER_UPGRADE_TRIGGER:-/run/kalinka-renderer/upgrade-request}"

# An empty or absent request means "whatever is newest", which is what the
# installer does with no argument. Anything else is passed through as the
# version to install; the installer rejects one it cannot find.
TARGET=""
if [ -r "$TRIGGER" ]; then
    TARGET="$(head -n 1 "$TRIGGER" | tr -dc 'A-Za-z0-9._-')"
fi

# A directory of its own: the installer runs, as root, any installer beside it.
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT
TMP="$DIR/install.sh"

echo ">> Fetching the renderer installer from $URL"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP"
else
    wget -qO "$TMP" "$URL"
fi

if [ -n "$TARGET" ]; then
    echo ">> Installing renderer $TARGET"
    bash "$TMP" "$TARGET"
else
    echo ">> Installing the latest published renderer"
    bash "$TMP"
fi
