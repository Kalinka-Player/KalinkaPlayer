#!/usr/bin/env bash
# Changes laptop networking. Passwords are entered only in the phone app.
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$pkg_dir/build/kalinka-supervisor" provision --backend nm --always-advertise "$@"
