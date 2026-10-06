#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
arch="${GOARCH:-$(dpkg --print-architecture)}"
case "$arch" in amd64|arm64) ;; *) echo 'Supported package architectures: amd64, arm64' >&2; exit 1;; esac
version="${VERSION:-0.1.0}"
out_dir="${OUT_DIR:-$pkg_dir/build}"
dpkg --validate-version "$version"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"
mkdir -p "$stage/DEBIAN" "$stage/usr/lib/kalinka-supervisor" "$stage/usr/lib/systemd/system" "$stage/usr/lib/udev/rules.d" "$out_dir"
GOARCH="$arch" VERSION="$version" "$pkg_dir/build.sh" "$stage/usr/lib/kalinka-supervisor/kalinka-supervisor"
install -m 644 "$pkg_dir/systemd/"* "$stage/usr/lib/systemd/system/"
install -m 644 "$pkg_dir/udev/"*.rules "$stage/usr/lib/udev/rules.d/"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: kalinka-supervisor
Version: $version
Architecture: $arch
Maintainer: Dmitry Savin <envelsavinds@gmail.com>
Depends: bluez, dbus, systemd, network-manager | ifupdown, wpasupplicant, iw, rfkill, iproute2
Description: Independent Kalinka supervisor and nearby box setup
 Static Go service for BLE provisioning with NetworkManager or DietPi networking.
CONTROL
cat > "$stage/DEBIAN/postinst" <<'HOOK'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
  # Replacing the experimental Python service must not leave two BLE owners.
  systemctl disable --now kalinka-provision.service 2>/dev/null || true
  systemctl daemon-reload
  systemctl enable kalinka-supervisor.service
  systemctl try-restart kalinka-supervisor.service
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
