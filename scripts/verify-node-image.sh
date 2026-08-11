#!/usr/bin/env bash
set -euo pipefail

NODE_IMAGE="${1:-termflow-node:verify}"

docker run --rm --user 0:0 --entrypoint /bin/sh "${NODE_IMAGE}" -euxc '
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

# Minimal-permission runtime smoke: PTY, HOME writes, and a live tmux
# session under cap-drop ALL + read-only rootfs, exactly as production
# recommends (the /home tmpfs must be mounted for uid/gid 1000 or the
# unprivileged termflow user cannot write its login state).
docker run --rm --cap-drop ALL --read-only \
  --tmpfs /tmp --tmpfs /home/termflow:uid=1000,gid=1000,mode=0750 \
  --entrypoint /bin/sh "${NODE_IMAGE}" -euxc '
  python -c "import pty; pty.openpty()"
  python -c "from pathlib import Path; p = Path.home()/\".config\"/\"termflow\"; p.mkdir(parents=True, exist_ok=True); (p/\"write-check\").write_text(\"ok\")"
  tmux new-session -d -s verify "sleep 30"
  tmux capture-pane -p -t verify >/dev/null
  tmux kill-session -t verify
'

# Default run smoke: without the recommended /home tmpfs, the image must
# still provide a writable HOME for the unprivileged termflow user.
docker run --rm --cap-drop ALL \
  --entrypoint /bin/sh "${NODE_IMAGE}" -euxc '
  test -w /home/termflow
  python -c "from pathlib import Path; p = Path.home()/\"write-check\"; p.write_text(\"ok\"); p.unlink()"
'

# termflow serve: a persistent compute node must run without a TTY, keep
# the same Term identity across container restarts, and stop cleanly on
# SIGTERM (docker stop) with no leftover Bridge.
NODE_CONTAINER="termflow-verify-node-$$"
NODE_VOLUME="${NODE_CONTAINER}-home"
cleanup() {
  docker rm --force "${NODE_CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${NODE_VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Seed a valid Installation login into the identity volume (0600, as
# termflow login would write it) before the service starts.
docker run --rm --user 0:0 \
  --volume "${NODE_VOLUME}:/home/termflow" \
  --entrypoint /bin/sh "${NODE_IMAGE}" -ec '
  mkdir -p /home/termflow/.config/termflow
  printf "%s\n" "{\"server_url\":\"http://127.0.0.1:1\",\"installation_id\":\"00000000-0000-0000-0000-000000000001\",\"installation_token\":\"dummy-token\",\"allow_insecure_http\":true}" > /home/termflow/.config/termflow/config.json
  chmod 600 /home/termflow/.config/termflow/config.json
  chown -R 1000:1000 /home/termflow
'

docker run --detach \
  --name "${NODE_CONTAINER}" \
  --cap-drop ALL \
  --volume "${NODE_VOLUME}:/home/termflow" \
  "${NODE_IMAGE}" \
  termflow serve --name demo >/dev/null

for attempt in $(seq 1 30); do
  if docker exec "${NODE_CONTAINER}" termflow status demo --json 2>/dev/null | grep -q '"bridge_alive":true'; then
    break
  fi
  sleep 1
done
first_status="$(docker exec "${NODE_CONTAINER}" termflow status demo --json)"
echo "${first_status}" | grep -q '"tmux_alive":true'
echo "${first_status}" | grep -q '"bridge_alive":true'
echo "${first_status}" | grep -q '"lifecycle":"running"'

# docker stop sends SIGTERM; the exit code must be 0 (clean), not 137.
docker stop --time 15 "${NODE_CONTAINER}" >/dev/null
test "$(docker inspect --format '{{.State.ExitCode}}' "${NODE_CONTAINER}")" = "0"

# Restart must preserve the same Installation/Term identity and never
# produce a duplicate Instance.
first_id="$(echo "${first_status}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
docker start "${NODE_CONTAINER}" >/dev/null
for attempt in $(seq 1 30); do
  second_status="$(docker exec "${NODE_CONTAINER}" termflow status demo --json 2>/dev/null)" || { sleep 1; continue; }
  echo "${second_status}" | grep -q '"bridge_alive":true' && break
  sleep 1
done
second_id="$(echo "${second_status}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
test "${second_id}" = "${first_id}"

single_instance="$(docker exec "${NODE_CONTAINER}" termflow list --json \
  | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')"
test "${single_instance}" = "1"
