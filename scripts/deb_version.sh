# shellcheck shell=bash
#
# The Debian version of a Python package's PEP 440 version, for the deb builds.
# A '.devN' build leads up to the next release, so it takes '~' to sort below
# it in dpkg as it does in PEP 440: 5.1.2.dev1+gcb9a628 -> 5.1.2~dev1+gcb9a628.

deb_version() {
    printf '%s\n' "$1" | sed 's/\.dev\([0-9]\)/~dev\1/'
}
