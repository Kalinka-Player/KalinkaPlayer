#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo 'usage: pull-base.sh <container-engine> <image>' >&2
  exit 2
fi

engine="$1"
images=("$2")
if [ "$2" = public.ecr.aws/docker/library/debian:trixie ]; then
  images+=(docker.io/library/debian:trixie)
fi

for image in "${images[@]}"; do
  if "$engine" image inspect "$image" >/dev/null 2>&1 || "$engine" pull "$image" >&2; then
    printf '%s\n' "$image"
    exit 0
  fi
  echo "Could not obtain base image: $image" >&2
done

exit 1
