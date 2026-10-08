#!/usr/bin/env bash
set -euo pipefail
pkg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$pkg_dir"
. "$pkg_dir/../../scripts/tag_version.sh"
version="${VERSION:-$(tag_version kalinka-supervisor-v 2>/dev/null || echo development)}"
output="${1:-$pkg_dir/build/kalinka-supervisor}"
mkdir -p "$(dirname "$output")"
CGO_ENABLED=0 GOOS=linux "${GO:-go}" build -mod=readonly -trimpath \
  -ldflags "-s -w -X main.version=$version" \
  -o "$output" ./cmd/kalinka-supervisor
