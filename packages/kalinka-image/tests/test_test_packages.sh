#!/usr/bin/env bash
set -uo pipefail
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
. "$TESTS_DIR/helpers.sh"
. "$PKG_DIR/lib/common.sh"
. "$PKG_DIR/lib/test-packages.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

make_package() {
  local destination="$1" package="$2" arch="$3" version="${4:-1.0}"
  mkdir -p "$WORK/package/DEBIAN" "$destination"
  cat > "$WORK/package/DEBIAN/control" <<EOF
Package: $package
Version: $version
Architecture: $arch
Maintainer: Test <test@example.org>
Description: Image test fixture
EOF
  dpkg-deb --root-owner-group --build "$WORK/package" \
    "$destination/${package}_${version}_${arch}.deb" >/dev/null
}

mkdir -p "$WORK/input"
for package in kalinka-server kalinka-plugin-sdk kalinka-plugin-bluetooth; do
  make_package "$WORK/input" "$package" all
done
make_package "$WORK/input" kalinka-renderer arm64
stage_test_packages "$WORK/input" "$WORK/staged" arm64
assert_eq "the bundle records every package" "$(wc -l < "$WORK/staged/packages.tsv")" 4
while IFS=$'\t' read -r package version arch filename checksum; do
  assert_eq "the staged bytes match their checksum" \
    "$(sha256sum "$WORK/staged/$filename" | cut -d ' ' -f1)" "$checksum"
done < "$WORK/staged/packages.tsv"

make_package "$WORK/wrong" kalinka-renderer amd64
message="$(stage_test_packages "$WORK/wrong" "$WORK/wrong-stage" arm64 2>&1)" \
  && fail "accepted an x86 renderer for a Pi"
assert_contains "the architecture error identifies the package" "$message" 'wrong architecture for kalinka-renderer'

message="$(stage_test_packages "$WORK/absent" "$WORK/absent-stage" arm64 2>&1)" \
  && fail "accepted a missing directory"
assert_contains "a missing directory is explained" "$message" 'not a directory'

make_package "$WORK/incomplete" kalinka-plugin-bluetooth all
message="$(stage_test_packages "$WORK/incomplete" "$WORK/incomplete-stage" arm64 2>&1)" \
  && fail "accepted the plugin without its matching Core"
assert_contains "a partial bundle identifies the missing dependency" "$message" 'missing test package: kalinka-server'

make_package "$WORK/input" kalinka-server all 2.0
message="$(stage_test_packages "$WORK/input" "$WORK/duplicate-stage" arm64 2>&1)" \
  && fail "accepted two Core versions"
assert_contains "duplicate versions are explained" "$message" 'more than one test package for kalinka-server'

ROOTFS="$WORK/root"
mkdir -p "$ROOTFS/etc/kalinka"
printf '{"base_config.display.enabled":true,"base_config.server.auto_upgrade":true}\n' \
  > "$ROOTFS/etc/kalinka/kalinka_conf.cfg"
configure_test_inputs "$ROOTFS" "$WORK/staged/packages.tsv"
python3 - "$ROOTFS/etc/kalinka/kalinka_conf.cfg" <<'PY' || fail 'test settings are wrong'
import json, sys
data = json.load(open(sys.argv[1]))
assert data['base_config.display.enabled'] is True
assert data['base_config.server.auto_upgrade'] is False
assert data['input_modules.bluetooth.enabled'] is True
assert 'input_modules.airplay.enabled' not in data
PY
assert_mode "test config remains private" "$ROOTFS/etc/kalinka/kalinka_conf.cfg" 600
printf 'kalinka-plugin-airplay\t1.0\tarm64\tairplay.deb\tunused\n' >> "$WORK/staged/packages.tsv"
configure_test_inputs "$ROOTFS" "$WORK/staged/packages.tsv"
python3 - "$ROOTFS/etc/kalinka/kalinka_conf.cfg" <<'PY' || fail 'included AirPlay was not enabled'
import json, sys
assert json.load(open(sys.argv[1]))['input_modules.airplay.enabled'] is True
PY

ROOTFS="$WORK/release-root"
unset IGconf_kalinka_test_debs
in_chroot() { fail "release build invoked test setup"; }
install_test_packages
verify_test_packages
[ ! -e "$ROOTFS" ] || fail "test setup changed a release image"
exit "$FAILURES"
