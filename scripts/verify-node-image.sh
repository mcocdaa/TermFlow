#!/usr/bin/env bash
set -euo pipefail

NODE_IMAGE="${1:-termflow-node:verify}"
NODE_RUNTIME_SECURITY_ARGS=(
  --cap-drop ALL
  --cap-add CHOWN
  --cap-add DAC_OVERRIDE
  --cap-add SETUID
  --cap-add SETGID
  --security-opt no-new-privileges:true
)

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

# Keep cleanup recoverable when a smoke test fails midway.
NODE_CONTAINER="termflow-verify-node-$$"
NODE_VOLUME="${NODE_CONTAINER}-home"
NODE_BIND_ROOT=""
cleanup() {
  docker rm --force "${NODE_CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${NODE_VOLUME}" >/dev/null 2>&1 || true
  if [[ -n "${NODE_BIND_ROOT}" && -d "${NODE_BIND_ROOT}" ]]; then
    docker run --rm --user 0:0 \
      --volume "${NODE_BIND_ROOT}:/mount-root" \
      --entrypoint /bin/sh "${NODE_IMAGE}" \
      -ec 'rm -rf /mount-root/identity /mount-root/work' >/dev/null 2>&1 || true
    rmdir "${NODE_BIND_ROOT}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

pane_shell() {
  local status_payload="$1"
  local socket_path
  socket_path="$(printf '%s' "${status_payload}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["socket_path"])')"
  docker exec --user termflow "${NODE_CONTAINER}" \
    tmux -S "${socket_path}" display-message -p -t demo:0.0 '#{pane_current_command}'
}

invalid_shell_output=""
if invalid_shell_output="$(docker run --rm \
  "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  --env TERMFLOW_SHELL=zsh \
  "${NODE_IMAGE}" true 2>&1)"; then
  echo "node entrypoint accepted an unsupported TERMFLOW_SHELL" >&2
  exit 1
fi
printf '%s\n' "${invalid_shell_output}" \
  | grep -Fq "invalid TERMFLOW_SHELL: expected bash or sh"

# Minimal-permission runtime smoke: the privileged init only receives the
# capabilities needed to normalize ownership and drop identity. The command
# that ultimately becomes PID 1 must be non-root with no effective capabilities.
docker run --rm "${NODE_RUNTIME_SECURITY_ARGS[@]}" --read-only \
  --tmpfs /tmp \
  --tmpfs /home/termflow:uid=1000,gid=1000,mode=0750 \
  --tmpfs /work:uid=1000,gid=1000,mode=0750 \
  "${NODE_IMAGE}" /bin/sh -euxc '
  test "$(stat -c %u /proc/1)" = "$(id -u termflow)"
  test "$(awk '\''$1 == "CapEff:" { print $2 }'\'' /proc/1/status)" = "0000000000000000"
  python -c "import pty; pty.openpty()"
  python -c "from pathlib import Path; p = Path.home()/\".config\"/\"termflow\"; p.mkdir(parents=True, exist_ok=True); (p/\"write-check\").write_text(\"ok\")"
  tmux new-session -d -s verify "sleep 30"
  tmux capture-pane -p -t verify >/dev/null
  tmux kill-session -t verify
'

# Default run smoke: without the recommended /home tmpfs, the image must
# still provide a writable HOME for the dropped termflow user.
docker run --rm "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  "${NODE_IMAGE}" /bin/sh -euxc '
  test -w /home/termflow
  python -c "from pathlib import Path; p = Path.home()/\"write-check\"; p.write_text(\"ok\"); p.unlink()"
'

# Symlinked mount points must be refused by the privileged init.
docker run --rm --user 0:0 --entrypoint /bin/sh "${NODE_IMAGE}" -ec '
  rm -rf /work
  ln -s /tmp /work
  if /usr/local/bin/termflow-entrypoint true >/dev/null 2>&1; then
    echo "entrypoint accepted a symlinked mount point" >&2
    exit 1
  fi
'

# Fresh root-owned bind mounts must become writable without host-side chown.
NODE_BIND_ROOT="$(mktemp -d)"
docker run --rm --user 0:0 \
  --volume "${NODE_BIND_ROOT}:/mount-root" \
  --entrypoint /bin/sh "${NODE_IMAGE}" -ec '
  mkdir /mount-root/identity /mount-root/work
  printf "%s\n" identity > /mount-root/identity/existing
  printf "%s\n" work > /mount-root/work/existing
  chmod 600 /mount-root/identity/existing /mount-root/work/existing
  test "$(stat -c %u /mount-root/identity)" = 0
  test "$(stat -c %u /mount-root/work)" = 0
'

docker run --rm "${NODE_RUNTIME_SECURITY_ARGS[@]}" --read-only \
  --tmpfs /tmp \
  --volume "${NODE_BIND_ROOT}/identity:/home/termflow" \
  --volume "${NODE_BIND_ROOT}/work:/work" \
  "${NODE_IMAGE}" /bin/sh -euxc '
  test "$(stat -c %u /proc/1)" = "$(id -u termflow)"
  test "$(awk '\''$1 == "CapEff:" { print $2 }'\'' /proc/1/status)" = "0000000000000000"
  test "$(stat -c %u /home/termflow/existing)" = "$(id -u termflow)"
  test "$(stat -c %u /work/existing)" = "$(id -u termflow)"
  printf "%s\n" ok > /home/termflow/write-check
  printf "%s\n" ok > /work/write-check
'

# termflow serve: a persistent compute node must run without a TTY, keep
# the same Term identity across container restarts, and stop cleanly on
# SIGTERM (docker stop) with no leftover Bridge.
# Seed a valid Installation login into the identity volume (0600, as
# termflow login would write it) before the service starts. Leave the
# seeded file root-owned so the real entrypoint has to initialize it.
docker run --rm --user 0:0 \
  --volume "${NODE_VOLUME}:/home/termflow" \
  --entrypoint /bin/sh "${NODE_IMAGE}" -ec '
  mkdir -p /home/termflow/.config/termflow
  printf "%s\n" "{\"server_url\":\"http://127.0.0.1:1\",\"installation_id\":\"00000000-0000-0000-0000-000000000001\",\"installation_token\":\"dummy-token\",\"allow_insecure_http\":true}" > /home/termflow/.config/termflow/config.json
  chmod 600 /home/termflow/.config/termflow/config.json
'

docker run --detach \
  --name "${NODE_CONTAINER}" \
  "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  --volume "${NODE_VOLUME}:/home/termflow" \
  "${NODE_IMAGE}" \
  termflow serve --name demo >/dev/null

for attempt in $(seq 1 30); do
  if docker exec --user termflow "${NODE_CONTAINER}" termflow status demo --json 2>/dev/null | grep -q '"bridge_alive":true'; then
    break
  fi
  sleep 1
done
first_status="$(docker exec --user termflow "${NODE_CONTAINER}" termflow status demo --json)"
echo "${first_status}" | grep -q '"tmux_alive":true'
echo "${first_status}" | grep -q '"bridge_alive":true'
echo "${first_status}" | grep -q '"lifecycle":"running"'
test "$(pane_shell "${first_status}")" = "bash"

# docker stop sends SIGTERM; the exit code must be 0 (clean), not 137.
docker stop --time 15 "${NODE_CONTAINER}" >/dev/null
test "$(docker inspect --format '{{.State.ExitCode}}' "${NODE_CONTAINER}")" = "0"

# Restart must preserve the same Installation/Term identity and never
# produce a duplicate Instance.
first_id="$(echo "${first_status}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
docker start "${NODE_CONTAINER}" >/dev/null
for attempt in $(seq 1 30); do
  second_status="$(docker exec --user termflow "${NODE_CONTAINER}" termflow status demo --json 2>/dev/null)" || { sleep 1; continue; }
  echo "${second_status}" | grep -q '"bridge_alive":true' && break
  sleep 1
done
second_id="$(echo "${second_status}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
test "${second_id}" = "${first_id}"

single_instance="$(docker exec --user termflow "${NODE_CONTAINER}" termflow list --json \
  | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')"
test "${single_instance}" = "1"

# Environment changes require container recreation. Preserve the identity
# volume, then prove the same Term comes back with POSIX sh as its pane shell.
docker stop --time 15 "${NODE_CONTAINER}" >/dev/null
docker rm "${NODE_CONTAINER}" >/dev/null
docker run --detach \
  --name "${NODE_CONTAINER}" \
  "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  --env TERMFLOW_SHELL=sh \
  --volume "${NODE_VOLUME}:/home/termflow" \
  "${NODE_IMAGE}" \
  termflow serve --name demo >/dev/null

third_status=""
for attempt in $(seq 1 30); do
  third_status="$(docker exec --user termflow "${NODE_CONTAINER}" \
    termflow status demo --json 2>/dev/null)" || { sleep 1; continue; }
  echo "${third_status}" | grep -q '"bridge_alive":true' && break
  sleep 1
done
third_id="$(echo "${third_status}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
test "${third_id}" = "${first_id}"
test "$(pane_shell "${third_status}")" = "sh"
