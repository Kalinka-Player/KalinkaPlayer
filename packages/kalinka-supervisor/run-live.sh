#!/usr/bin/env bash
# Changes laptop networking. Passwords are entered only in the phone app.
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# As root the control API would reboot this laptop for real.
export KALINKA_CONTROL_API=0
exec "$pkg_dir/build/kalinka-supervisor" --backend nm --always-advertise "$@"
