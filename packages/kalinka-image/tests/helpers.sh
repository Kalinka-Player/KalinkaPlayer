# shellcheck shell=bash
# Assertions and fixtures shared by the image test files.

FAILURES=0

fail() { echo "    FAIL: $*"; FAILURES=$((FAILURES + 1)); }

assert_eq() {
  [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"
}

assert_contains() {
  case "$2" in *"$3"*) ;; *) fail "$1: '$2' does not contain '$3'" ;; esac
}

assert_not_contains() {
  case "$2" in *"$3"*) fail "$1: '$2' unexpectedly contains '$3'" ;; esac
}

assert_file() {
  [ -f "$2" ] || fail "$1: no file at $2"
}

assert_no_file() {
  [ -e "$2" ] && fail "$1: $2 still exists"
  return 0
}

assert_mode() {
  local actual
  actual="$(stat -c %a "$2" 2>/dev/null || echo missing)"
  [ "$actual" = "$3" ] || fail "$1: $2 is mode $actual, expected $3"
}

# A directory of executables that record every call to $CALL_LOG instead of
# doing anything, so a script can be run without the system it talks to.
make_recorders() {
  RECORDER_BIN="$(mktemp -d)"
  CALL_LOG="$RECORDER_BIN/calls"
  : > "$CALL_LOG"
  local name
  for name in "$@"; do _write_recorder "$name" 0; done
  PATH="$RECORDER_BIN:$PATH"
}

# Turns recorders make_recorders already made into ones that also fail.
make_failing_recorders() {
  local name
  for name in "$@"; do _write_recorder "$name" 1; done
}

_write_recorder() {
  cat > "$RECORDER_BIN/$1" <<RECORDER
#!/bin/sh
echo "$1 \$*" >> "$CALL_LOG"
exit $2
RECORDER
  chmod 755 "$RECORDER_BIN/$1"
}

calls() { cat "$CALL_LOG"; }

needs_disposable_system() {
  if [ "${KALINKA_IMAGE_TEST_DISPOSABLE:-0}" != 1 ] || [ "$(id -u)" -ne 0 ]; then
    echo "    SKIP: needs KALINKA_IMAGE_TEST_DISPOSABLE=1 and root — it edits /etc"
    exit 77
  fi
}
