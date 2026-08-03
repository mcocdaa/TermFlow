#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 || -z "$1" ]]; then
  echo "usage: verify_control_plane_release_image.sh IMAGE" >&2
  exit 2
fi

IMAGE="$1"
SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/../.." && pwd)"
PROJECT="termflow-release-image-test-$$"

export TERMFLOW_IMAGE="${IMAGE}"
export TERMFLOW_ADMIN_TOKEN="release-test-admin-token-which-is-long-enough"
export TERMFLOW_HOST_PORT="18076"
export TERMFLOW_DATA_VOLUME="${PROJECT}-data"
export TERMFLOW_PUBLIC_BASE_URL="http://127.0.0.1:${TERMFLOW_HOST_PORT}"
export TERMFLOW_TRUSTED_WEB_ORIGINS="${TERMFLOW_PUBLIC_BASE_URL}"

cleanup() {
  docker compose -p "${PROJECT}" -f "${REPOSITORY_ROOT}/deploy/compose.yaml" \
    down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose -p "${PROJECT}" -f "${REPOSITORY_ROOT}/deploy/compose.yaml" up -d --wait
curl --fail --silent --show-error http://127.0.0.1:18076/healthz
