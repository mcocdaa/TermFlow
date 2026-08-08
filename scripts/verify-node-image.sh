#!/usr/bin/env bash
set -euo pipefail

NODE_IMAGE="${1:-termflow-node:verify}"

docker run --rm --user 0:0 --entrypoint /bin/sh "${NODE_IMAGE}" -eu -c '
  test -x /opt/termflow/bin/termflow
  /opt/termflow/bin/termflow --version
  command -v tmux >/dev/null 2>&1
  tmux -V | grep -Eq "tmux 3\.[2-9]"
  command -v ps >/dev/null 2>&1
  command -v curl >/dev/null 2>&1
  /opt/termflow/bin/python -c "import importlib.util, termflow_node, termflow_protocol; assert importlib.util.find_spec(\"termflow_control_plane\") is None"

  for forbidden_tree in /workspace /build /wheels /src /tests /app/apps /app/packages
  do
    test ! -e "${forbidden_tree}"
  done

  forbidden_files="$(find / -xdev -type f \( \
    -name package.json -o -name package-lock.json -o -name yarn.lock -o \
    -name pnpm-lock.yaml -o -name Cargo.toml -o -name Cargo.lock -o \
    -name uv.lock -o -name pyproject.toml -o -name \*.ts -o -name \*.tsx -o \
    -name \*.vue -o -name \*.whl \
  \) -print)"
  test -z "${forbidden_files}"

  for forbidden_command in node npm cargo rustc uv
  do
    ! command -v "${forbidden_command}" >/dev/null 2>&1
  done
'

# Minimal-permission runtime smoke: PTY and a live tmux session under
# cap-drop ALL + read-only rootfs, exactly as production recommends.
docker run --rm --cap-drop ALL --read-only --tmpfs /tmp --tmpfs /home/termflow \
  --entrypoint /bin/sh "${NODE_IMAGE}" -eu -c '
  python -c "import pty; pty.openpty()"
  tmux new-session -d -s verify "sleep 30"
  tmux capture-pane -p -t verify >/dev/null
  tmux kill-session -t verify
'
