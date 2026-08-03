#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 || -z "$1" ]]; then
  echo "usage: verify_control_plane_release_image.sh IMAGE" >&2
  exit 2
fi

IMAGE="$1"
CONTAINER="termflow-release-image-test-$$"
VOLUME="${CONTAINER}-data"
HOST_PORT="18076"
HEALTH_URL="http://127.0.0.1:18076/healthz"

cleanup() {
  docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "${VOLUME}" >/dev/null
docker run --detach \
  --name "${CONTAINER}" \
  --pull never \
  --publish "127.0.0.1:${HOST_PORT}:8000" \
  --mount "source=${VOLUME},target=/app/data" \
  --env TERMFLOW_ADMIN_TOKEN=release-test-admin-token-which-is-long-enough \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK=true \
  --env "TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:${HOST_PORT}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/data/totp-master-key \
  "${IMAGE}" >/dev/null

for attempt in $(seq 1 60); do
  if curl --fail --silent "${HEALTH_URL}"; then
    exit 0
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}")" != "true" ]]; then
    docker logs "${CONTAINER}" >&2
    exit 1
  fi
  sleep 1
done

docker logs "${CONTAINER}" >&2
echo "control-plane release image did not become healthy" >&2
exit 1
