#!/usr/bin/env bash
#
# Run install-release.sh from this checkout on each throwaway system, one
# container apiece, keeping each one's output in a log. The checkout's server
# and renderer maintainer scripts go in beside it. `make install-test` picks
# the engine and the Debian 13 image.
#
# Usage: run.sh <container-engine> <debian-13-image>
# Env:   INSTALL_TEST_LOGS  where the logs go (default: tmp/install-test)
set -uo pipefail

if [ "$#" -ne 2 ]; then
  echo 'usage: run.sh <container-engine> <debian-13-image>' >&2
  exit 2
fi

engine="$1" image="$2"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
logs="${INSTALL_TEST_LOGS:-$root/tmp/install-test}"
mkdir -p "$logs" || exit 1

failed=()
for system in debian dietpi; do
  log="$logs/$system.log"
  echo "== $system: install-release.sh on a throwaway $image via ${engine##*/}"
  if "$engine" run --pull=never --rm \
      -v "$root/scripts:/kalinka/scripts:ro,z" \
      -v "$root/tests/install:/kalinka/tests/install:ro,z" \
      -v "$root/packages/kalinka-server/DEBIAN:/kalinka/packages/kalinka-server/DEBIAN:ro,z" \
      -v "$root/packages/kalinka-renderer/DEBIAN:/kalinka/packages/kalinka-renderer/DEBIAN:ro,z" \
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
