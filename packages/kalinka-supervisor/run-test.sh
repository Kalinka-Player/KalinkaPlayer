#!/usr/bin/env bash
# Real Bluetooth, simulated Wi-Fi and control actions. Build before running (including before sudo).
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$pkg_dir/build/kalinka-supervisor" --test "$@"
