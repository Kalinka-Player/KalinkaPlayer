# shellcheck shell=bash
# What the chroot needs while packages go in, taken out again before sealing. Defines only.

start_build_aids() {
  cat > "$ROOTFS/etc/apt/preferences.d/kalinka-image-excludes" <<PREFERENCES
Package: ${EXCLUDED_PACKAGES[*]}
Pin: release *
Pin-Priority: -1
PREFERENCES
  # fpcalc is a Recommends a base may turn off; by-source upgrades would move the kernel with linux-libc-dev.
  printf 'APT::Install-Recommends "true";\nAPT::Get::Upgrade-By-Source-Package "false";\n' \
    > "$ROOTFS/etc/apt/apt.conf.d/99kalinka-image-build"

  # No systemd runs in a chroot; build-aids/systemctl says what stands in for it.
  [ ! -e "$ROOTFS/usr/sbin/policy-rc.d" ] || die "the base system already has a policy-rc.d"
  printf '#!/bin/sh\nexit 101\n' > "$ROOTFS/usr/sbin/policy-rc.d"
  chmod 755 "$ROOTFS/usr/sbin/policy-rc.d"
  in_chroot dpkg-divert --local --rename --divert /usr/bin/systemctl.real \
    --add /usr/bin/systemctl
  install -m 755 "$SCRIPT_DIR/build-aids/systemctl" "$ROOTFS/usr/bin/systemctl"
}

stop_build_aids() {
  rm -f "$ROOTFS/usr/bin/systemctl"
  in_chroot dpkg-divert --local --rename --divert /usr/bin/systemctl.real \
    --remove /usr/bin/systemctl
  rm -f "$ROOTFS/usr/sbin/policy-rc.d"
  # The refusals kept the build lean; they do not bind the machine's owner.
  rm -f "$ROOTFS/etc/apt/preferences.d/kalinka-image-excludes" \
    "$ROOTFS/etc/apt/apt.conf.d/99kalinka-image-build"
}

# The image's own resolv.conf, file or symlink, comes back exactly as it was.
use_build_dns() {
  local conf="$ROOTFS/etc/resolv.conf" saved="$WORK/resolv.conf.image"
  rm -f "$saved"
  if [ -e "$conf" ] || [ -L "$conf" ]; then
    cp -a "$conf" "$saved"
    rm -f "$conf"
  fi
  # The chroot shares the host's network, where public resolvers may be blocked but its own works.
  cat /etc/resolv.conf > "$conf"
}

restore_dns() {
  local conf="$ROOTFS/etc/resolv.conf" saved="$WORK/resolv.conf.image"
  rm -f "$conf"
  if [ -e "$saved" ] || [ -L "$saved" ]; then
    cp -a "$saved" "$conf"
  fi
}
