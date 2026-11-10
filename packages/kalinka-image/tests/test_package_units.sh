#!/usr/bin/env bash
#
# The server's and the renderer's postinsts, run behind the build's systemctl
# as the image build runs them: no systemd is running, and nothing in the build
# enables kalinka.service or kalinka-renderer.service but these scripts. If
# either stops enabling them there, the image boots with nothing to play.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$TESTS_DIR")"
# shellcheck source=helpers.sh
. "$TESTS_DIR/helpers.sh"
for lib in common chroot-aids; do
  # shellcheck source=/dev/null
  . "$PKG_DIR/lib/$lib.sh"
done

needs_disposable_system

PACKAGES="$(dirname "$PKG_DIR")"
SCRIPT_DIR="$PKG_DIR"
ROOTFS=/
ASIDE="$(mktemp -d)"
AIDS=""
UNITS=()

cleanup() {
  # Before start_build_aids, the systemctl stop_build_aids removes is systemd's own.
  [ -z "$AIDS" ] || stop_build_aids
  [ ! -e "$ASIDE/policy-rc.d" ] || mv "$ASIDE/policy-rc.d" /usr/sbin/policy-rc.d
  rm -f "${UNITS[@]}" /etc/systemd/system/*.wants/kalinka* \
    /var/lib/systemd/deb-systemd-helper-enabled/kalinka* \
    /var/lib/systemd/deb-systemd-helper-enabled/*.wants/kalinka*
  rm -rf "$ASIDE"
}
trap cleanup EXIT

[ -x /usr/bin/systemctl ] || { fail "no systemctl from the systemd package to stand in front of"; exit "$FAILURES"; }
[ ! -d /run/systemd/system ] || { fail "systemd runs here, which it never does in the build"; exit "$FAILURES"; }

# The build's base has none of its own; this container's may.
[ ! -e /usr/sbin/policy-rc.d ] || mv /usr/sbin/policy-rc.d "$ASIDE/policy-rc.d"
AIDS=1
start_build_aids

echo "  -- the units where the packages put them"
mkdir -p /etc/systemd/system /usr/lib/systemd/system
for unit in "$PACKAGES"/kalinka-server/scripts/*.{service,path,socket}; do
  install -m 644 "$unit" /etc/systemd/system/
  UNITS+=("/etc/systemd/system/${unit##*/}")
done
for unit in "$PACKAGES"/kalinka-renderer/scripts/*.{service,path,timer}; do
  install -m 644 "$unit" /usr/lib/systemd/system/
  UNITS+=("/usr/lib/systemd/system/${unit##*/}")
done

echo "  -- each postinst configures"
for package in kalinka-server kalinka-renderer; do
  # deb-systemd-helper runs only for dpkg, which it tells by this.
  output="$(in_chroot env DPKG_MAINTSCRIPT_PACKAGE="$package" DPKG_MAINTSCRIPT_NAME=postinst \
    bash "$PACKAGES/$package/DEBIAN/postinst" configure 2>&1)"
  status=$?
  assert_eq "$package's postinst succeeds" "$status" 0
  assert_contains "and starts nothing" "$output" "so nothing was started"
  [ "$status" -eq 0 ] || echo "$output"
done

echo "  -- and leaves its units enabled"
( require_enabled multi-user.target kalinka.service kalinka-renderer.service ) \
  || fail "the image would boot without the server or the renderer"
( require_enabled multi-user.target kalinka-restart.path kalinka-plugins.path \
    kalinka-upgrade.path kalinka-mounts.path kalinka-renderer-upgrade.path ) \
  || fail "the image would boot without the server's or the renderer's watchers"
( require_enabled sockets.target kalinka-journal-reader.socket ) \
  || fail "the image would boot without the journal reader"

exit "$FAILURES"
