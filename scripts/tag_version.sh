# shellcheck shell=bash
#
# X.Y.Z at a clean <prefix>X.Y.Z tag, else X.Y.(Z+1)~dev<commits>+g<sha>[.d<date>]:
# '~' sorts below the release it leads up to, the count above earlier builds.

_tag_version_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

tag_version() {
    local prefix=$1
    # Image builds run as root over a user's checkout, which git would refuse.
    local -a git=(git -C "$_tag_version_repo" -c "safe.directory=$_tag_version_repo")
    local tag base count dirty

    if ! "${git[@]}" rev-parse --git-dir > /dev/null 2>&1; then
        echo "tag_version: $_tag_version_repo is not a git checkout; pass the version explicitly" >&2
        return 1
    fi
    if ! tag=$("${git[@]}" describe --tags --abbrev=0 --match "$prefix*" 2> /dev/null); then
        echo "tag_version: no $prefix* tag reachable from HEAD (a shallow clone needs fetch-depth: 0)" >&2
        return 1
    fi

    base=${tag#"$prefix"}
    if ! [[ $base =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "tag_version: $tag is not $prefix<major>.<minor>.<patch>" >&2
        return 1
    fi
    count=$("${git[@]}" rev-list --count "$tag..HEAD")
    dirty=""
    if [ -n "$("${git[@]}" --no-optional-locks status --porcelain)" ]; then
        dirty=".d$(date +%y%m%d)"
    fi

    if [ "$count" -eq 0 ] && [ -z "$dirty" ]; then
        printf '%s\n' "$base"
        return
    fi
    printf '%s.%s~dev%s+g%s%s\n' "${base%.*}" "$(( ${base##*.} + 1 ))" \
        "$count" "$("${git[@]}" rev-parse --short HEAD)" "$dirty"
}
