#!/usr/bin/env bash
set -euo pipefail

CONTROL_PLANE_IMAGE="${1:-termflow-control-plane:verify}"

docker run --rm --user 0:0 --entrypoint /bin/sh "${CONTROL_PLANE_IMAGE}" -eu -c '
  test -x /opt/termflow/bin/termflow-control
  test -f /app/frontend-dist/index.html
  /opt/termflow/bin/python -c "import importlib.util, termflow_control_plane, termflow_protocol; assert importlib.util.find_spec(\"termflow_node\") is None"
  /opt/termflow/bin/termflow-control auth totp reset --help >/dev/null

  for forbidden_path in \
    /app/apps \
    /app/packages \
    /app/src \
    /app/tests \
    /app/pyproject.toml \
    /app/uv.lock \
    /app/package.json \
    /app/package-lock.json \
    /app/Cargo.toml \
    /app/Cargo.lock
  do
    test ! -e "${forbidden_path}"
  done

  forbidden_files="$(find / -xdev -type f \( \
    -name package.json -o -name package-lock.json -o -name yarn.lock -o \
    -name pnpm-lock.yaml -o -name Cargo.toml -o -name Cargo.lock -o \
    -name uv.lock -o -name pyproject.toml -o -name \*.ts -o -name \*.tsx -o \
    -name \*.vue \
  \) -print)"
  test -z "${forbidden_files}"

  for forbidden_tree in /workspace /build /src /tests /app/apps /app/packages
  do
    test ! -e "${forbidden_tree}"
  done

  for forbidden_command in node npm cargo rustc uv
  do
    ! command -v "${forbidden_command}" >/dev/null 2>&1
  done
'

# The entrypoint initializes mount points as root and then exec-drops to
# termflow: PID 1 must never run as uid 0.
docker run --rm "${CONTROL_PLANE_IMAGE}" sh -ec '
  test "$(stat -c %u /proc/1)" = "$(id -u termflow)"
  test "$(id -u termflow)" != 0
'

# A --user override bypasses the privileged init and still works.
test "$(docker run --rm --user "$(id -u):$(id -g)" "${CONTROL_PLANE_IMAGE}" id -u)" = "$(id -u)"

# Symlinked mount points must be refused by the privileged init.
docker run --rm --user 0:0 --entrypoint /bin/sh "${CONTROL_PLANE_IMAGE}" -ec '
  rm -rf /app/data
  ln -s /tmp /app/data
  if /usr/local/bin/termflow-control-entrypoint true >/dev/null 2>&1; then
    echo "entrypoint accepted a symlinked mount point" >&2
    exit 1
  fi
'

# Fresh root-owned bind mounts must work with zero host-side chown: the
# database and the TOTP master key are created and owned by termflow.
CONTROL_PLANE_CONTAINER="termflow-verify-control-plane-$$"
CONTROL_PLANE_HOST_PORT="18077"
CONTROL_PLANE_HEALTH_URL="http://127.0.0.1:${CONTROL_PLANE_HOST_PORT}/healthz"
DATA_DIR="$(mktemp -d)"
TOTP_DIR="$(mktemp -d)"
cleanup() {
  docker rm --force "${CONTROL_PLANE_CONTAINER}" >/dev/null 2>&1 || true
  docker run --rm --user 0:0 \
    --volume "${DATA_DIR}:/data" \
    --volume "${TOTP_DIR}:/totp" \
    --entrypoint rm "${CONTROL_PLANE_IMAGE}" -rf /data /totp >/dev/null 2>&1 || true
  rmdir "${DATA_DIR}" "${TOTP_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

docker run --detach \
  --name "${CONTROL_PLANE_CONTAINER}" \
  --publish "127.0.0.1:${CONTROL_PLANE_HOST_PORT}:8000" \
  --env TERMFLOW_ADMIN_TOKEN=verify-admin-token-that-is-long-enough \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK=true \
  --env "TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:${CONTROL_PLANE_HOST_PORT}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/totp-secrets/totp-master-key \
  --volume "${DATA_DIR}:/app/data" \
  --volume "${TOTP_DIR}:/app/totp-secrets" \
  "${CONTROL_PLANE_IMAGE}" >/dev/null

for attempt in $(seq 1 60); do
  if curl --fail --silent "${CONTROL_PLANE_HEALTH_URL}" >/dev/null; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTROL_PLANE_CONTAINER}")" != "true" ]]; then
    docker logs "${CONTROL_PLANE_CONTAINER}" >&2
    exit 1
  fi
  sleep 1
done

docker exec "${CONTROL_PLANE_CONTAINER}" sh -ec '
  test -f /app/data/termflow.db
  test -f /app/totp-secrets/totp-master-key
  test "$(stat -c %u /app/data/termflow.db)" = "$(id -u termflow)"
  test "$(stat -c %u /app/totp-secrets/totp-master-key)" = "$(id -u termflow)"
  test "$(stat -c %u /app/data)" = "$(id -u termflow)"
  test "$(stat -c %u /app/totp-secrets)" = "$(id -u termflow)"
'
curl --fail --silent "${CONTROL_PLANE_HEALTH_URL}" >/dev/null
