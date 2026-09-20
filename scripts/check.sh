#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"
echo "==> [TermFlow] Client contracts check..."
npm run contracts:check
echo "==> [TermFlow] Backend ruff, mypy & pytest..."
uv run --frozen ruff check .
uv run --frozen --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
PYTHONPATH=. uv run --frozen python -m pytest -q --ignore=tests/e2e
echo "==> [TermFlow] Frontend typecheck & test..."
npm run typecheck
npm run test
echo "✓ 全部检查通过 (TermFlow)"
