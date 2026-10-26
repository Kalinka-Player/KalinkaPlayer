#!/bin/bash
# Kalinka's part of an rpi-image-gen image, run on the root it has built: the
# steps build-image.sh takes after the DietPi base is in place.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOTFS="$1"
TARGET_ARCH=arm64
TARGET_BASE=rpi-image-gen
TARGET_DISPLAY=0
[ "${IGconf_kalinka_display:-n}" != y ] || TARGET_DISPLAY=1

# shellcheck source=../lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=../lib/chroot-aids.sh
. "$SCRIPT_DIR/lib/chroot-aids.sh"
# shellcheck source=../lib/kalinka.sh
. "$SCRIPT_DIR/lib/kalinka.sh"
# shellcheck source=../lib/supervisor.sh
. "$SCRIPT_DIR/lib/supervisor.sh"
# shellcheck source=../lib/display.sh
. "$SCRIPT_DIR/lib/display.sh"

# build.sh hands the token over as a file: rpi-image-gen prints every setting it is given.
[ -z "${IGconf_kalinka_github_token_file:-}" ] || GITHUB_TOKEN="$(cat "$IGconf_kalinka_github_token_file")"

WORK="$(mktemp -d)"
# mmdebstrap holds a policy-rc.d of its own until it finishes, and removes it then.
mmdebstrap_policy="$WORK/policy-rc.d.mmdebstrap"
restore_mmdebstrap_policy() {
  [ ! -e "$mmdebstrap_policy" ] || mv -f "$mmdebstrap_policy" "$ROOTFS/usr/sbin/policy-rc.d"
}
trap 'restore_mmdebstrap_policy; rm -rf "$WORK"' EXIT
mv "$ROOTFS/usr/sbin/policy-rc.d" "$mmdebstrap_policy"

limit_journal
printf '[Journal]\nStorage=persistent\n' > "$ROOTFS/etc/systemd/journald.conf.d/kalinka-persistent.conf"
start_build_aids
configure_display_setting "$ROOTFS"
install_kalinka "${IGconf_kalinka_version:-}"
verify_kalinka
cp "$IGconf_kalinka_supervisor_deb" "$WORK/kalinka-supervisor.deb"
install_supervisor
verify_supervisor
verify_display
# What went in, for build.sh to name the image and write its manifest.
printf '%s %s %s %s\n' "$(in_chroot dpkg-query -W -f='${Version}' kalinka-server)" \
  "$(in_chroot dpkg-query -W -f='${Version}' kalinka-supervisor)" \
  "$(in_chroot dpkg-query -W -f='${Version}' kalinka-renderer)" "$TARGET_DISPLAY" > "$IGconf_kalinka_contents"
stop_build_aids
restore_mmdebstrap_policy

# The supervisor turns SSH on when someone sets a password for it.
in_chroot systemctl disable ssh.service
# Keys baked in would be every box's; the openssh-server layer has each make its own on first boot.
! compgen -G "$ROOTFS/etc/ssh/ssh_host_*" > /dev/null || die 'the image carries SSH host keys'
require_enabled multi-user.target ssh-hostkeys-generate.service
# The card's space past the image's few gigabytes stays unallocated until growroot fills it, as on the PC image.
install -D -m 755 "$SCRIPT_DIR/overlays/debootstrap/usr/lib/kalinka-image/growroot.sh" "$ROOTFS/usr/lib/kalinka-image/growroot.sh"
install -D -m 644 "$SCRIPT_DIR/overlays/debootstrap/usr/lib/systemd/system/kalinka-growroot.service" "$ROOTFS/usr/lib/systemd/system/kalinka-growroot.service"
in_chroot systemctl enable kalinka-growroot.service
require_enabled multi-user.target kalinka-growroot.service
rm -rf "$ROOTFS/var/cache/kalinka"
