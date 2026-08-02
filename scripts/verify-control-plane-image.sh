#!/usr/bin/env bash
set -euo pipefail

CONTROL_PLANE_IMAGE="${1:-termflow-control-plane:verify}"

docker run --rm --entrypoint /bin/sh "${CONTROL_PLANE_IMAGE}" -eu -c '
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
