#!/usr/bin/env bash
set -euo pipefail

CONTROL_PLANE_IMAGE="${1:-termflow-control-plane:verify}"

docker run --rm --entrypoint /bin/sh "${CONTROL_PLANE_IMAGE}" -eu -c '
  test -x /opt/termflow/bin/termflow-control
  test -f /app/frontend-dist/index.html
  /opt/termflow/bin/python -c "import termflow_control_plane, termflow_protocol"

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

  for forbidden_command in node npm cargo rustc uv
  do
    ! command -v "${forbidden_command}" >/dev/null 2>&1
  done
'
