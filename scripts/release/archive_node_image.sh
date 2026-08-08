#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 || -z "$1" || -z "$2" ]]; then
  echo "usage: archive_node_image.sh IMAGE OUTPUT_TAR" >&2
  exit 2
fi

IMAGE="$1"
OUTPUT_TAR="$2"
TEMPORARY_TAR="${OUTPUT_TAR}.tmp"
if [[ -e "$OUTPUT_TAR" || -e "$TEMPORARY_TAR" ]]; then
  echo "refusing to replace an existing image archive: $OUTPUT_TAR" >&2
  exit 2
fi
trap 'rm -f "$TEMPORARY_TAR"' EXIT

before_id="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
docker save --output "$TEMPORARY_TAR" "$IMAGE"
docker image rm "$IMAGE" >/dev/null
docker load --input "$TEMPORARY_TAR" >/dev/null
after_id="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
if [[ "$after_id" != "$before_id" ]]; then
  echo "node image identity changed after save/load" >&2
  exit 1
fi
mv "$TEMPORARY_TAR" "$OUTPUT_TAR"
