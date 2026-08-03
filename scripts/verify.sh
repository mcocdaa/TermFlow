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
uv run --all-packages pytest -q
uv run --all-packages ruff check .
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
scripts/verify-tauri.sh

CONTROL_PLANE_IMAGE="${TERMFLOW_VERIFY_IMAGE:-termflow-control-plane:verify}"
TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough" \
  docker compose -f deploy/compose.yaml config --quiet
scripts/build-control-plane-image.sh "${CONTROL_PLANE_IMAGE}"
scripts/verify-control-plane-image.sh "${CONTROL_PLANE_IMAGE}"
