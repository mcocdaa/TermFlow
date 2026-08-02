#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPOSITORY_ROOT}"

npm ci
npm run contracts:check
npm run test:run
npm run typecheck
npm run build:web
uv sync --frozen --all-packages
uv run --all-packages pytest -q
uv run --all-packages ruff check .
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
docker compose -f deploy/compose.yaml config --quiet
