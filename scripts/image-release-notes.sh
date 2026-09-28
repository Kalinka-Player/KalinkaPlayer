#!/usr/bin/env bash
#
# Print the notes for a kalinka-image-v* release: what each image is for, how
# to check a download, and links into the installation guide for everything
# else. Writing an image and setting it up are the guide's to explain; a copy
# of those steps in the notes had already drifted from it.
#
# The links point at the guide as it stands at the tag, so their anchors keep
# resolving whatever later becomes of its headings.
#
# Usage: image-release-notes.sh <kalinka-image-vX.Y.Z>
set -euo pipefail

tag="${1:?usage: image-release-notes.sh <kalinka-image-vX.Y.Z>}"
guide="https://github.com/Kalinka-Player/KalinkaPlayer/blob/$tag/docs/installation.md"

cat <<EOF
| Image | For | Built on |
|---|---|---|
| \`…-rpi234-arm64.img.xz\` | Raspberry Pi 3, 4, 400, Zero 2 W, CM3 and CM4 | DietPi (Debian 13) |
| \`…-rpi5-arm64.img.xz\` | Raspberry Pi 5, 500 and CM5 | DietPi (Debian 13) |
| \`…-amd64.img.xz\` | Any x86-64 PC or virtual machine, UEFI or BIOS | Debian 13 |

Verify downloads with \`sha256sum -c SHA256SUMS --ignore-missing\`.

To write an image and start it, follow [Install the server]($guide#install-the-server). For Wi-Fi, a login or a DAC HAT, see [Settings on the card]($guide#settings-on-the-card).
EOF
