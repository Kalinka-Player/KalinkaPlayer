#!/usr/bin/env bash
#
# Run install-release.sh from this checkout on each throwaway system, one
# container apiece from the image named for it, keeping each one's output in
# a log. `make install-test` picks the engine and the images.
#
# Usage: run.sh <container-engine> <system>=<image>...
# Env:   INSTALL_TEST_LOGS  where the logs go (default: tmp/install-test)
set -uo pipefail

usage() {
  echo 'usage: run.sh <container-engine> <system>=<image>...' >&2
  exit 2
}

[ "$#" -ge 2 ] || usage
engine="$1"
shift
for pair in "$@"; do
  case "$pair" in
    ?*=?*) ;;
    *) usage ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
logs="${INSTALL_TEST_LOGS:-$root/tmp/install-test}"
mkdir -p "$logs" || exit 1

failed=()
for pair in "$@"; do
  system="${pair%%=*}" image="${pair#*=}"
  log="$logs/$system.log"
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
