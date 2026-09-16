#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPOSITORY_ROOT}"

EXPECTED_NODE_VERSION="v22.23.2"
if [[ "$(node --version)" != "${EXPECTED_NODE_VERSION}" ]]; then
  echo "TermFlow verification requires Node ${EXPECTED_NODE_VERSION}; found $(node --version)." >&2
  exit 1
fi

npm ci
npm run contracts:check
npm run test:run
npm run typecheck
npm run build --workspaces --if-present
uv sync --frozen --all-packages
uv run --frozen --all-packages python -m pytest -q
uv run --frozen --all-packages ruff check .
uv run --frozen --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
scripts/verify-tauri.sh

CONTROL_PLANE_IMAGE="${TERMFLOW_VERIFY_IMAGE:-termflow-control-plane:verify}"
TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough" \
  OPENCODE_SERVER_USERNAME="termflow" \
  OPENCODE_SERVER_PASSWORD="verify-opencode-password" \
  OPENCODE_AGENT_MCP_TOKEN="verify-agent-mcp-token" \
  docker compose -f deploy/compose.yaml config --quiet
TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough" \
  OPENCODE_SERVER_USERNAME="termflow" \
  OPENCODE_SERVER_PASSWORD="verify-opencode-password" \
  OPENCODE_AGENT_MCP_TOKEN="verify-agent-mcp-token" \
  DEEPSEEK_API_KEY="verify-deepseek-key" \
  TERMFLOW_RELEASE_IMAGE_TAG="verify" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN="https://api.deepseek.com" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS="deepseek-v4-flash" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION="global" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS="account policy" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION="policy-2026-09" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING="true" \
  TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION="policy-2026-09" \
  docker compose -f deploy/compose.release.yaml config --quiet
scripts/build-control-plane-image.sh "${CONTROL_PLANE_IMAGE}"
scripts/verify-control-plane-image.sh "${CONTROL_PLANE_IMAGE}"

# Runtime container inspection is intentionally opt-in: it requires a running
# Compose project and must never start or remove user services as a side effect
# of the normal static verification sweep.
if [[ "${TERMFLOW_VERIFY_AGENT_CONTAINERS:-0}" == "1" ]]; then
  if [[ -n "${TERMFLOW_SECURITY_PROJECT:-}" && -n "${TERMFLOW_SECURITY_MODE:-}" ]]; then
    scripts/security/verify-agent-containers.sh --mode "${TERMFLOW_SECURITY_MODE}" "${TERMFLOW_SECURITY_PROJECT}"
  else
    echo "set TERMFLOW_SECURITY_PROJECT and TERMFLOW_SECURITY_MODE=offline|live" >&2
    exit 2
  fi
fi
