# shellcheck shell=bash
#
# RENDERER_VERSION wins: the release workflow passes the tag it is building.

# shellcheck source=../../../scripts/tag_version.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../../scripts/tag_version.sh"

renderer_version() {
    if [ -n "${RENDERER_VERSION:-}" ]; then
        printf '%s\n' "$RENDERER_VERSION"
        return
    fi
    tag_version kalinka-renderer-v
}
