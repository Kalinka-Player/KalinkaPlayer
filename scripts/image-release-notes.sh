#!/usr/bin/env bash
#
# Print the notes for a kalinka-image-v* release: which image is for which
# hardware, what the Pi images carry, how to check a download, and links into
# the installation guide for everything else. Writing an image and setting it
# up are the guide's to explain; a copy of those steps in the notes had already
# drifted from it.
#
# The links point at the guide as it stands at the tag, so their anchors keep
# resolving whatever later becomes of its headings.
#
# Usage: bash scripts/image-release-notes.sh kalinka-image-vX.Y.Z > notes.md
set -euo pipefail

tag="${1:-}"
case "$tag" in
  kalinka-image-v?*) ;;
  *)
    echo "usage: $0 kalinka-image-vX.Y.Z" >&2
    exit 2
    ;;
esac

# Quoted, so the backticks stay Markdown.
sed "s|/blob/main/docs/|/blob/$tag/docs/|g" <<'EOF'
Ready-to-flash images of Kalinka Player with the server, every first-party plugin, the browser player and the renderer already installed and enabled. Power one on, open `http://<its-ip>:8000` in a browser, and it plays — no install step, no login needed.

| Image | For | Built on |
|---|---|---|
| `…-rpi234-arm64.img.xz` | Raspberry Pi 3, 4, 400, Zero 2 W, CM3 and CM4 | DietPi (Debian 13) |
| `…-rpi5-arm64.img.xz` | Raspberry Pi 5, 500 and CM5 | DietPi (Debian 13) |
| `…-amd64.img.xz` | x86-64 PC or virtual machine, UEFI (Secure Boot off) | DietPi (Debian 13) |

The Raspberry Pi images play through DAC HATs as well as USB DACs. A HAT with an ID chip is set up by itself.

To flash an image and start it, follow [Install the server](https://github.com/Kalinka-Player/KalinkaPlayer/blob/main/docs/installation.md#install-the-server). For Wi-Fi, a login or a DAC HAT without an ID chip, change [Settings on the card](https://github.com/Kalinka-Player/KalinkaPlayer/blob/main/docs/installation.md#settings-on-the-card) before the first start.

On all images, DietPi's update notices stay on, its survey is off, and logs are kept on disk rather than in memory.

Verify downloads with `sha256sum -c SHA256SUMS --ignore-missing`.
EOF
