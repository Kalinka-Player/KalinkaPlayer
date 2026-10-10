#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
arch="${GOARCH:-$(dpkg --print-architecture)}"
case "$arch" in amd64|arm64) ;; *) echo 'Supported package architectures: amd64, arm64' >&2; exit 1;; esac
. "$pkg_dir/../../scripts/tag_version.sh"
version="${VERSION:-$(tag_version kalinka-supervisor-v)}"
out_dir="${OUT_DIR:-$pkg_dir/build}"
dpkg --validate-version "$version"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"
mkdir -p "$stage/DEBIAN" "$stage/usr/lib/kalinka-supervisor" "$stage/usr/lib/systemd/system" "$out_dir"
GOARCH="$arch" VERSION="$version" "$pkg_dir/build.sh" "$stage/usr/lib/kalinka-supervisor/kalinka-supervisor"
install -m 755 "$pkg_dir/reinstall.sh" "$pkg_dir/ssh-setup.sh" "$stage/usr/lib/kalinka-supervisor/"
install -m 644 "$pkg_dir/systemd/"* "$stage/usr/lib/systemd/system/"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: kalinka-supervisor
Version: $version
Architecture: $arch
Maintainer: Dmitry Savin <envelsavinds@gmail.com>
Depends: bluez, dbus, systemd, curl, network-manager | ifupdown, wpasupplicant, iw, rfkill, iproute2, passwd, sudo
Description: Independent Kalinka supervisor, nearby box setup and LAN control page
 Static Go service for BLE provisioning with NetworkManager or DietPi networking,
 and a recovery page on the local network: status, restart, reboot, power off
 and reinstall.
CONTROL
cat > "$stage/DEBIAN/postinst" <<'HOOK'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
  # Replacing the experimental Python service must not leave two BLE owners.
  systemctl disable --now kalinka-provision.service 2>/dev/null || true
  systemctl daemon-reload
  # Older packages also linked the unit into radio targets; reenable drops those links.
  systemctl reenable kalinka-supervisor.service || true
  # Core's upgrade installs this package under set -e: a start failure must not abort it.
  systemctl --no-block restart kalinka-supervisor.service || true
fi
HOOK
cat > "$stage/DEBIAN/prerm" <<'HOOK'
#!/bin/sh
set -e
if [ "$1" = remove ] && [ -d /run/systemd/system ]; then
  systemctl disable --now kalinka-supervisor.service
fi
HOOK
cat > "$stage/DEBIAN/postrm" <<'HOOK'
#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
HOOK
chmod 755 "$stage/DEBIAN/"postinst "$stage/DEBIAN/"prerm "$stage/DEBIAN/"postrm
dpkg-deb --root-owner-group --build "$stage" "$out_dir/kalinka-supervisor_${version}_${arch}.deb"
