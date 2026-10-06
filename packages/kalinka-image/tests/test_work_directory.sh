#!/usr/bin/env bash
set -euo pipefail
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$TESTS_DIR/../lib/common.sh"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/keep" "$stage/discard" "$stage/success"
run_build() (
  IMAGE_WORK_DIR="$stage/$1"
  IMAGE_KEEP_FAILED="$2"
  start_work
  touch "$WORK/marker"
  exit "$3"
)
if run_build keep 1 7; then exit 1; else [ "$?" = 7 ]; fi
compgen -G "$stage/keep/kalinka-image.*/marker" >/dev/null
if run_build discard 0 9; then exit 1; else [ "$?" = 9 ]; fi
! compgen -G "$stage/discard/kalinka-image.*" >/dev/null
run_build success 1 0
! compgen -G "$stage/success/kalinka-image.*" >/dev/null
echo 'Failed-build retention preserves files and exit status; successful scratch is removed'
