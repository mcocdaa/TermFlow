#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPOSITORY_ROOT}"

uv sync --frozen --all-packages
uv run --all-packages pytest -q
uv run --all-packages ruff check .
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
docker compose -f deploy/compose.yaml config --quiet
