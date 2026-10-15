#!/usr/bin/env bash
set -euo pipefail
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$TESTS_DIR/helpers.sh"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
export CALL_LOG="$stage/calls" CACHED_IMAGE='' PULLABLE_IMAGE=''
mirror=public.ecr.aws/docker/library/debian:trixie
upstream=docker.io/library/debian:trixie
custom=example.invalid/custom:tag

cat > "$stage/engine" <<'ENGINE'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
case "$1" in
  image) [ "$2" = inspect ] && [ "$3" = "$CACHED_IMAGE" ] ;;
  pull)
    echo "pull output for $2"
    [ "$2" = "$PULLABLE_IMAGE" ]
    ;;
  *) exit 99 ;;
esac
ENGINE
chmod 755 "$stage/engine"

select_base() { bash "$TESTS_DIR/pull-base.sh" "$stage/engine" "$1" 2> "$stage/stderr"; }
reset_engine() { : > "$CALL_LOG"; CACHED_IMAGE=''; PULLABLE_IMAGE=''; }

reset_engine
CACHED_IMAGE="$mirror"
assert_eq 'cached mirror selected' "$(select_base "$mirror")" "$mirror"
assert_eq 'cached mirror requires no pull' "$(cat "$CALL_LOG")" "image inspect $mirror"

reset_engine
PULLABLE_IMAGE="$mirror"
assert_eq 'mirror download selected without pull output' "$(select_base "$mirror")" "$mirror"
assert_not_contains 'available mirror needs no fallback' "$(cat "$CALL_LOG")" "$upstream"

reset_engine
PULLABLE_IMAGE="$upstream"
assert_eq 'failed mirror falls back' "$(select_base "$mirror")" "$upstream"
assert_eq 'fallback follows the failed pull' "$(cat "$CALL_LOG")" "$(printf 'image inspect %s\npull %s\nimage inspect %s\npull %s' "$mirror" "$mirror" "$upstream" "$upstream")"

reset_engine
CACHED_IMAGE="$upstream"
assert_eq 'cached fallback selected' "$(select_base "$mirror")" "$upstream"
assert_not_contains 'cached fallback needs no pull' "$(cat "$CALL_LOG")" "pull $upstream"

reset_engine
if selected="$(select_base "$mirror")"; then
  fail 'both unavailable registries must fail'
fi
assert_eq 'no image returned after failure' "$selected" ''
assert_contains 'fallback failure reported' "$(cat "$stage/stderr")" "$upstream"

reset_engine
PULLABLE_IMAGE="$upstream"
if selected="$(select_base "$custom")"; then
  fail 'a custom base must not fall back to Debian'
fi
assert_eq 'failed custom image has no substitute' "$selected" ''
assert_not_contains 'custom base never tries the default' "$(cat "$CALL_LOG")" "$upstream"

reset_engine
PULLABLE_IMAGE="$custom"
assert_eq 'custom base remains supported' "$(select_base "$custom")" "$custom"

exit "$FAILURES"
