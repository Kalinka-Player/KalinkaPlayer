#!/usr/bin/env bash
#
# Run install-release.sh from this checkout on each throwaway system, one
# container apiece from the image named for it, keeping each one's output in
# a log. An image is pulled when its system's turn comes, so one that cannot
# be had fails only its own system. `make install-test` picks the engine and
# the images.
#
# Usage: run.sh <container-engine> <system>=<image>...
# Env:   INSTALL_TEST_LOGS  where the logs go (default: tmp/install-test)
set -uo pipefail

# The systems system.sh can make, and cases.py has cases for.
systems=(debian dietpi old-python)

refuse() {
  echo "run.sh: $*" >&2
  echo 'usage: run.sh <container-engine> <system>=<image>...' >&2
  exit 2
}

[ "$#" -ge 2 ] || refuse "no system to run"
engine="$1"
shift
declare -A named=()
for pair in "$@"; do
  case "$pair" in
    ?*=?*) ;;
    *) refuse "'$pair' is not <system>=<image>" ;;
  esac
  system="${pair%%=*}"
  [[ " ${systems[*]} " == *" $system "* ]] || refuse "no system '$system': there are ${systems[*]}"
  [ -z "${named[$system]:-}" ] || refuse "$system is named twice"
  named[$system]=1
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
logs="${INSTALL_TEST_LOGS:-$root/tmp/install-test}"
mkdir -p "$logs" || exit 1

failed=()
for pair in "$@"; do
  system="${pair%%=*}" base="${pair#*=}"
  log="$logs/$system.log"
  if ! image="$(bash "$root/packages/kalinka-image/tests/pull-base.sh" "$engine" "$base")"; then
    echo "could not obtain $base" | tee "$log"
    echo "== $system: FAILED, its log is $log"
    failed+=("$system")
    continue
  fi
  echo "== $system: install-release.sh on a throwaway $image via ${engine##*/}"
  if "$engine" run --pull=never --rm \
      -v "$root/scripts:/kalinka/scripts:ro,z" \
      -v "$root/tests/install:/kalinka/tests/install:ro,z" \
      -e DEBIAN_FRONTEND=noninteractive -e GITHUB_TOKEN -e KALINKA_REPO -e KALINKA_WEB_REPO \
      "$image" bash /kalinka/tests/install/system.sh "$system" 2>&1 | tee "$log"; then
    echo "== $system: passed"
  else
    echo "== $system: FAILED, its log is $log"
    failed+=("$system")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "install-test failed on: ${failed[*]}"
  exit 1
fi
echo "install-test passed"
