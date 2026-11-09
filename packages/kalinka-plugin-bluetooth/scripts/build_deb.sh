#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. ../../scripts/deb_version.sh
rm -rf pkgroot dist
./scripts/build_wheel.sh
wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
version=$(basename "$wheel" | sed 's/kalinka_plugin_bluetooth-\(.*\)-py3-none-any.whl/\1/')
version=$(deb_version "$version")
install -d pkgroot/opt/kalinka/wheels pkgroot/DEBIAN
install -m 644 "$wheel" pkgroot/opt/kalinka/wheels/
sed "s/@VERSION@/$version/g" debian/control.in > pkgroot/DEBIAN/control
install -m 644 debian/triggers pkgroot/DEBIAN/triggers
install -m 755 debian/postinst debian/prerm debian/postrm pkgroot/DEBIAN/
install -D -m 755 packaging/start-bluealsa pkgroot/usr/lib/kalinka-plugin-bluetooth/start-bluealsa
install -D -m 644 packaging/kalinka-bluealsa.service pkgroot/usr/lib/systemd/system/kalinka-bluealsa.service
install -D -m 644 packaging/kalinka-bluetooth-dbus.conf pkgroot/usr/share/dbus-1/system.d/kalinka-bluetooth.conf
install -D -m 644 packaging/bluetooth.conf pkgroot/usr/lib/tmpfiles.d/kalinka-bluetooth.conf
dpkg-deb --root-owner-group --build pkgroot "kalinka-plugin-bluetooth_${version}_all.deb"
