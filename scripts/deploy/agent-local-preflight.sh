#!/usr/bin/env bash
set -euo pipefail

# Validate local deployment inputs without evaluating or echoing the secrets in
# the env file.  This is deliberately an evidence gate, not a lifecycle tool.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
BASE_COMPOSE="${REPOSITORY_ROOT}/deploy/compose.yaml"
LIVE_COMPOSE="${REPOSITORY_ROOT}/deploy/compose.agent-live.yaml"
OPENCODE_CONFIG="${REPOSITORY_ROOT}/deploy/opencode-config.yaml"
PROXY_CONFIG="${REPOSITORY_ROOT}/deploy/provider-egress/squid.conf"

usage() {
  echo "usage: $0 --env-file <absolute-path>" >&2
}

if [[ $# != 2 || "${1:-}" != "--env-file" ]]; then
  usage
  exit 2
fi

ENV_FILE="$2"
if [[ "${ENV_FILE}" != /* ]]; then
  echo "--env-file must be an absolute path" >&2
  exit 2
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "env file must be a regular file" >&2
  exit 1
fi
if [[ "$(stat -c '%a' "${ENV_FILE}")" != "600" ]]; then
  echo "env file must have mode 0600" >&2
  exit 1
fi

for required_file in "${BASE_COMPOSE}" "${LIVE_COMPOSE}" "${OPENCODE_CONFIG}" "${PROXY_CONFIG}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "$(basename "${required_file}") must be a regular file" >&2
    exit 1
  fi
done

dotenv_report="$(awk '
  # Parse a deliberately small, safe dotenv grammar.  Values are retained
  # only inside awk so diagnostics can never echo a credential.
  function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
  function issue(kind, key) { print kind ":" key; failed = 1 }
  /^[[:space:]]*($|#)/ { next }
  {
    line = trim($0)
    if (line ~ /^export[[:space:]]+/) sub(/^export[[:space:]]+/, "", line)
    if (line !~ /^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/) { issue("invalid", "dotenv"); next }
    key = line; sub(/[[:space:]]*=.*/, "", key)
    if (seen[key]++) { issue("duplicate", key); next }
    value = line; sub(/^[^=]*=[[:space:]]*/, "", value); value = trim(value)
    if (value ~ /^"/) {
      if (value !~ /^".*"$/) { issue("invalid", key); next }
      sub(/^"/, "", value); sub(/"$/, "", value)
    } else if (value ~ /^\047/) {
      if (value !~ /^\047.*\047$/) { issue("invalid", key); next }
      sub(/^\047/, "", value); sub(/\047$/, "", value)
    }
    values[key] = value
  }
  END {
    required[1]="TERMFLOW_ADMIN_TOKEN"
    required[2]="OPENCODE_SERVER_USERNAME"
    required[3]="OPENCODE_SERVER_PASSWORD"
    required[4]="DEEPSEEK_API_KEY"
    required[5]="OPENCODE_AGENT_MCP_TOKEN"
    required[6]="TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN"
    required[7]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN"
    required[8]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS"
    required[9]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION"
    required[10]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS"
    required[11]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION"
    required[12]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING"
    required[13]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE"
    required[14]="TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION"
    for (i = 1; i <= 14; i++) {
      key = required[i]
      if (!(key in values) || values[key] == "") issue("missing", key)
      else if (tolower(values[key]) ~ /(replace-with|fixture|example|changeme|deployment-secret|never-print|operator-supplied|deployment-unverified|unverified|todo|tbd|unknown)/)
        issue("placeholder", key)
    }
    if (("TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING" in values) && values["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING"] != "true")
      issue("invalid", "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING")
    if (("TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE" in values) && values["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE"] != "DEEPSEEK_API_KEY")
      issue("invalid", "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE")
    if (("TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN" in values) && values["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN"] != "https://api.deepseek.com")
      issue("invalid", "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN")
    if (("TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS" in values) && values["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS"] != "deepseek-v4-flash")
      issue("invalid", "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS")
    sensitive[1]="TERMFLOW_ADMIN_TOKEN"
    sensitive[2]="OPENCODE_SERVER_PASSWORD"
    sensitive[3]="DEEPSEEK_API_KEY"
    sensitive[4]="OPENCODE_AGENT_MCP_TOKEN"
    sensitive[5]="TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN"
    for (i = 1; i <= 5; i++) {
      key = sensitive[i]
      if (!(key in values)) continue
      for (j = i + 1; j <= 5; j++) {
        other = sensitive[j]
        if ((other in values) && values[key] != "" && values[key] == values[other]) issue("reused", other)
      }
    }
    if (failed) exit 1
    for (key in values) print "present:" key
  }
' "${ENV_FILE}")" || {
  # Every line emitted by awk contains only a variable name or the generic
  # dotenv marker; forwarding it is safe and useful to the operator.
  while IFS= read -r issue; do
    case "${issue}" in
      missing:*) echo "missing required variable: ${issue#missing:}" >&2 ;;
      invalid:*) echo "invalid value for: ${issue#invalid:}" >&2 ;;
      duplicate:*) echo "duplicate variable: ${issue#duplicate:}" >&2 ;;
      placeholder:*) echo "placeholder value rejected for: ${issue#placeholder:}" >&2 ;;
      reused:*) echo "sensitive credentials must be distinct (check: ${issue#reused:})" >&2 ;;
      *) echo "dotenv parse failed" >&2 ;;
    esac
  done <<<"${dotenv_report}"
  exit 1
}

# Stable physical resource names make a deployment auditable and make it
# impossible for an accidental project name change to select a prior volume.
if ! command -v docker >/dev/null; then
  echo "docker is required" >&2
  exit 2
fi
docker version --format '{{.Server.Version}}' >/dev/null

docker compose --env-file "${ENV_FILE}" -p termflow-v020-local \
  -f "${BASE_COMPOSE}" config --quiet
docker compose --env-file "${ENV_FILE}" -p termflow-v020-local \
  -f "${BASE_COMPOSE}" -f "${LIVE_COMPOSE}" config --quiet
rendered="$(docker compose --env-file "${ENV_FILE}" -p termflow-v020-local \
  -f "${BASE_COMPOSE}" -f "${LIVE_COMPOSE}" config --format json)"
jq -e '
  .name == "termflow-v020-local"
  # B serves Web C and the A/C API over a published host port.  The default
  # network must therefore remain a normal bridge; only capability-specific
  # backend networks are internal.
  and ((.networks.default.internal // false) == false)
  and .networks.agent_internal.internal == true
  and .networks.provider_egress.internal == true
  # Compose omits an explicit false from JSON; omitted and false both mean a
  # normal uplink network, while true must fail this gate.
  and ((.networks.provider_uplink.internal // false) == false)
  and .services["opencode-agent"].networks == {"agent_internal":null,"provider_egress":null}
  and .services["provider-egress-proxy"].networks == {"provider_egress":null,"provider_uplink":null}
  and ((.services["provider-egress-proxy"].ports // []) | length == 0)
  and ([.volumes[]?.name] | sort) == ["termflow-v020-local-data","termflow-v020-local-opencode-data","termflow-v020-local-totp-key"]
  and .services["control-plane"].environment.TERMFLOW_AGENT_OPENCODE_MCP_TOKEN == .services["opencode-agent"].environment.TERMFLOW_AGENT_MCP_TOKEN
  and .services["control-plane"].environment.TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE == "DEEPSEEK_API_KEY"
' >/dev/null <<<"${rendered}" || { echo "rendered Compose topology is unsafe" >&2; exit 1; }

echo "agent-local-preflight: PASS"
