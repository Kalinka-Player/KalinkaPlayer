#!/bin/bash
# The renderer image's part of an rpi-image-gen build, run on the root it has
# built: the renderer alone, for a box that plays for a server elsewhere.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOTFS="$1"

# shellcheck source=../lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=../lib/chroot-aids.sh
. "$SCRIPT_DIR/lib/chroot-aids.sh"
# shellcheck source=../lib/card-settings.sh
. "$SCRIPT_DIR/lib/card-settings.sh"
# shellcheck source=../lib/renderer-image.sh
. "$SCRIPT_DIR/lib/renderer-image.sh"

# build.sh hands the token over as a file: rpi-image-gen prints every setting it is given.
[ -z "${IGconf_kalinka_github_token_file:-}" ] || GITHUB_TOKEN="$(cat "$IGconf_kalinka_github_token_file")"

WORK="$(mktemp -d)"
trap 'restore_mmdebstrap_policy; rm -rf "$WORK"' EXIT
hold_mmdebstrap_policy

start_build_aids
install_renderer_only "${IGconf_kalinka_renderer_deb:-}"
stop_build_aids
ignore_imager_first_run
restore_mmdebstrap_policy

in_chroot dpkg-query -W -f='${Version}\n' kalinka-renderer > "$IGconf_kalinka_contents"
