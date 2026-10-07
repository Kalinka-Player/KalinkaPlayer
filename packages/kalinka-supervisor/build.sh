#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$pkg_dir"
output="${1:-$pkg_dir/build/kalinka-supervisor}"
mkdir -p "$(dirname "$output")"
CGO_ENABLED=0 GOOS=linux "${GO:-go}" build -mod=readonly -trimpath \
  -ldflags "-s -w -X main.version=${VERSION:-development}" \
  -o "$output" ./cmd/kalinka-supervisor
