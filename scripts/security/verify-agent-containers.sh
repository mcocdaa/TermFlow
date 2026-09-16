#!/usr/bin/env bash
set -euo pipefail

# Read-only runtime evidence gate. Compose labels identify exact existing
# containers; no `docker compose` lifecycle or configuration command is used.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
usage() { echo "usage: $0 --mode offline|live <exact-compose-project>" >&2; }
if [[ $# != 3 || "$1" != "--mode" || ( "$2" != offline && "$2" != live ) ]]; then usage; exit 2; fi
MODE="$2"
PROJECT="$3"
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

fail() { echo "$1: security assertion failed" >&2; exit 1; }
inspect() { docker inspect --format "$2" "$1"; }

container_id() {
  local service="$1"
  local -a ids=()
  # Network inspect keys are full 64-character IDs.  Request the same shape
  # here so exact member-set comparisons cannot fail open or fail spuriously.
  mapfile -t ids < <(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=${service}")
  if (( ${#ids[@]} != 1 )) || [[ -z "${ids[0]:-}" ]]; then
    echo "${service}: expected exactly one container for project ${PROJECT}" >&2
    exit 1
  fi
  printf '%s\n' "${ids[0]}"
}

labels_are_exact() {
  local service="$1" id="$2" labels
  labels="$(inspect "${id}" '{{json .Config.Labels}}')"
  jq -e --arg project "${PROJECT}" --arg service "${service}" \
    '.["com.docker.compose.project"] == $project and .["com.docker.compose.service"] == $service' \
    >/dev/null <<<"${labels}" || fail "${service} labels"
}
scalar_is() { [[ "$(inspect "$2" "$3")" == "$4" ]] || fail "$1"; }
json_has() { jq -e --arg x "$4" 'index($x) != null' >/dev/null <<<"$(inspect "$2" "$3")" || fail "$1"; }
json_is() { jq -e --argjson expected "$4" '. == $expected' >/dev/null <<<"$(inspect "$2" "$3")" || fail "$1"; }
json_is_absent_or_empty() { jq -e '. == null or . == []' >/dev/null <<<"$(inspect "$2" "$3")" || fail "$1"; }
security_profiles_are_confined() {
  jq -e 'all(.[]?; contains("unconfined") | not)' \
    >/dev/null <<<"$(inspect "$2" '{{json .HostConfig.SecurityOpt}}')" \
    || fail "$1 security profile"
}
tmpfs_has() { jq -e --arg x "$3" 'has($x)' >/dev/null <<<"$(inspect "$2" '{{json .HostConfig.Tmpfs}}')" || fail "$1"; }
no_host_ports() { jq -e 'all(.[]?; . == null)' >/dev/null <<<"$(inspect "$2" '{{json .NetworkSettings.Ports}}')" || fail "$1"; }
positive_limits() {
  local pids memory cpus
  pids="$(inspect "$2" '{{.HostConfig.PidsLimit}}')"
  memory="$(inspect "$2" '{{.HostConfig.Memory}}')"
  cpus="$(inspect "$2" '{{.HostConfig.NanoCpus}}')"
  [[ "$pids" =~ ^[1-9][0-9]*$ && "$memory" =~ ^[1-9][0-9]*$ && "$cpus" =~ ^[1-9][0-9]*$ ]] || fail "$1 limits"
}
networks_are_exact() {
  local service="$1" id="$2" expected
  shift 2
  expected="$(printf '%s\n' "$@" | jq -R . | jq -s 'sort')"
  jq -e --argjson expected "$expected" 'keys | sort == $expected' \
    >/dev/null <<<"$(inspect "$id" '{{json .NetworkSettings.Networks}}')" || fail "$service network topology"
}
network_contract() {
  local network="$1" internal="$2" expected_label="$3"
  local raw labels actual expected
  raw="$(docker network inspect --format '{{.Internal}}|{{json .Labels}}|{{json .Containers}}' "$network")"
  actual="${raw%%|*}"; raw="${raw#*|}"; labels="${raw%%|*}"; raw="${raw#*|}"
  [[ "$actual" == "$internal" ]] || fail "$network internal"
  jq -e --arg project "$PROJECT" --arg name "$expected_label" \
    '.["com.docker.compose.project"] == $project and .["com.docker.compose.network"] == $name' \
    >/dev/null <<<"$labels" || fail "$network labels"
  expected="$(printf '%s\n' "${@:4}" | jq -R . | jq -s 'sort')"
  jq -e --argjson expected "$expected" 'keys | sort == $expected' >/dev/null <<<"$raw" \
    || fail "$network members"
}
# The default network is B's host-facing A/C ingress network.  A may be
# attached by a separately managed Docker A acceptance fixture, so unlike the
# capability-specific networks above its member set is intentionally not fixed
# here.  Internal=false is required for Docker's explicit host publish to work.
network_metadata_contract() {
  local network="$1" internal="$2" expected_label="$3"
  local raw labels actual
  raw="$(docker network inspect --format '{{.Internal}}|{{json .Labels}}|{{json .Containers}}' "$network")"
  actual="${raw%%|*}"; raw="${raw#*|}"; labels="${raw%%|*}"
  [[ "$actual" == "$internal" ]] || fail "$network internal"
  jq -e --arg project "$PROJECT" --arg name "$expected_label" \
    '.["com.docker.compose.project"] == $project and .["com.docker.compose.network"] == $name' \
    >/dev/null <<<"$labels" || fail "$network labels"
}
mounts_are_exact() {
  local service="$1" id="$2" kind="$3" mounts
  mounts="$(inspect "$id" '{{json .Mounts}}')"
  local expected_volume="${PROJECT}-opencode-data"
  local opencode_source="${REPOSITORY_ROOT}/deploy/opencode-config.yaml"
  if [[ "$kind" == agent ]]; then
    jq -e --arg volume "$expected_volume" --arg source "$opencode_source" '
      length == 2
      and any(.[]; .Type == "volume" and .Name == $volume and .Destination == "/data" and .RW)
      and any(.[]; .Type == "bind" and .Source == $source and .Destination == "/etc/termflow/opencode-config.yaml" and (.RW | not))
    ' \
      >/dev/null <<<"$mounts" || fail "$service mounts"
  else
    jq -e --arg volume "$expected_volume" '
      length == 1
      and .[0].Type == "volume"
      and .[0].Name == $volume
      and .[0].Destination == "/data"
      and .[0].RW
    ' >/dev/null <<<"$mounts" || fail "$service mounts"
  fi
  jq -e 'all(.[]?; (.Source // "") | test("docker\\.sock$") | not)' >/dev/null <<<"$mounts" || fail "$service docker socket"
}
forbidden_env_names_absent() {
  local service="$1" id="$2" pattern="$3" names
  names="$(inspect "$id" '{{json .Config.Env}}' | jq 'map(split("=")[0])')"
  jq -e --arg pattern "$pattern" 'all(.[]?; test($pattern) | not)' >/dev/null <<<"$names" || fail "$service secret env"
}
no_proxy_environment() {
  local service="$1" id="$2" names
  names="$(inspect "$id" '{{json .Config.Env}}' | jq 'map(split("=")[0])')"
  jq -e 'all(.[]?; test("^(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|no_proxy|NO_PROXY)$") | not)' \
    >/dev/null <<<"$names" || fail "$service proxy env"
}
runtime_hardening() {
  local service="$1" id="$2" user="$3" image
  image="$(inspect "$id" '{{.Config.Image}}')"
  [[ "$image" == *@sha256:* ]] || fail "$service image digest"
  scalar_is "$service user" "$id" '{{.Config.User}}' "$user"
  scalar_is "$service rootfs" "$id" '{{.HostConfig.ReadonlyRootfs}}' true
  scalar_is "$service privileged" "$id" '{{.HostConfig.Privileged}}' false
  json_has "$service capabilities" "$id" '{{json .HostConfig.CapDrop}}' ALL
  # Docker Engine has represented an omitted CapAdd as either JSON null or
  # an empty array across releases. Both mean no capability was added; any
  # actual entry still fails closed.
  json_is_absent_or_empty "$service cap-add" "$id" '{{json .HostConfig.CapAdd}}'
  json_has "$service nnp" "$id" '{{json .HostConfig.SecurityOpt}}' no-new-privileges:true
  security_profiles_are_confined "$service" "$id"
  no_host_ports "$service ports" "$id"
}

agent_id="$(container_id opencode-agent)"
init_id="$(container_id opencode-init)"
labels_are_exact opencode-agent "$agent_id"
labels_are_exact opencode-init "$init_id"
runtime_hardening opencode-agent "$agent_id" 405:100
scalar_is 'opencode-agent running' "$agent_id" '{{.State.Running}}' true
tmpfs_has 'opencode-agent tmpfs' "$agent_id" /tmp
tmpfs_has 'opencode-agent home' "$agent_id" /home/opencode
positive_limits opencode-agent "$agent_id"
jq -e 'any(.[]?; .Name == "nofile" and .Soft == 1024 and .Hard == 1024)' \
  >/dev/null <<<"$(inspect "$agent_id" '{{json .HostConfig.Ulimits}}')" || fail 'opencode-agent ulimits'
jq -e 'all(.[]?; (.Source // "") | test("docker\\.sock$") | not)' \
  >/dev/null <<<"$(inspect "$agent_id" '{{json .Mounts}}')" || fail 'opencode-agent docker socket'
mounts_are_exact opencode-agent "$agent_id" agent
forbidden_env_names_absent opencode-agent "$agent_id" '^(TERMFLOW_ADMIN_TOKEN|TERMFLOW_DATABASE_URL|TERMFLOW_TOTP_MASTER_KEY|TERMFLOW_TOTP_MASTER_KEY_FILE|TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE|TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN|TERMFLOW_AGENT_OPENCODE_PASSWORD|ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENCODE_MODEL_API_KEY)$'

[[ "$(inspect "$init_id" '{{.Config.Image}}')" == *@sha256:* ]] || fail 'opencode-init image digest'
scalar_is 'opencode-init network' "$init_id" '{{.HostConfig.NetworkMode}}' none
scalar_is 'opencode-init user' "$init_id" '{{.Config.User}}' 0:0
scalar_is 'opencode-init stopped' "$init_id" '{{.State.Running}}' false
scalar_is 'opencode-init exit code' "$init_id" '{{.State.ExitCode}}' 0
scalar_is 'opencode-init rootfs' "$init_id" '{{.HostConfig.ReadonlyRootfs}}' true
scalar_is 'opencode-init privileged' "$init_id" '{{.HostConfig.Privileged}}' false
json_has 'opencode-init capabilities' "$init_id" '{{json .HostConfig.CapDrop}}' ALL
jq -e '
  type == "array"
  and map(sub("^CAP_"; "")) == ["CHOWN"]
' >/dev/null <<<"$(inspect "$init_id" '{{json .HostConfig.CapAdd}}')" \
  || fail 'opencode-init exact chown cap-add'
json_has 'opencode-init nnp' "$init_id" '{{json .HostConfig.SecurityOpt}}' no-new-privileges:true
security_profiles_are_confined opencode-init "$init_id"
no_host_ports 'opencode-init ports' "$init_id"
positive_limits opencode-init "$init_id"
mounts_are_exact opencode-init "$init_id" init
forbidden_env_names_absent opencode-init "$init_id" '(TOKEN|KEY|PASSWORD|SECRET)'

if [[ "$MODE" == offline ]]; then
  networks_are_exact opencode-agent "$agent_id" "${PROJECT}_agent_internal"
  control_id="$(container_id control-plane)"
  network_contract "${PROJECT}_agent_internal" true agent_internal "$agent_id" "$control_id"
  network_metadata_contract "${PROJECT}_default" false default
else
  control_id="$(container_id control-plane)"
  # The live profile grants the runtime its own uplink network for direct
  # provider TLS.  The frozen OpenCode permission map - not a network
  # allowlist - is the tool-use control; the accepted boundary and its
  # rationale live in docs/security.md ("运行时出网边界").
  networks_are_exact opencode-agent "$agent_id" "${PROJECT}_agent_internal" "${PROJECT}_provider_uplink"
  network_contract "${PROJECT}_agent_internal" true agent_internal "$agent_id" "$control_id"
  network_metadata_contract "${PROJECT}_default" false default
  network_contract "${PROJECT}_provider_uplink" false provider_uplink "$agent_id"
  no_proxy_environment opencode-agent "$agent_id"
fi

echo "agent-container-security: PASS"
echo "mode=${MODE} project=${PROJECT}"
echo "opencode-agent=${agent_id} opencode-init=${init_id}"
