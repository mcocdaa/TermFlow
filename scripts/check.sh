#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"
echo "==> [TermFlow] Backend ruff & pytest..."
uv run ruff check .
uv run pytest -q --ignore=tests/e2e
echo "==> [TermFlow] Frontend typecheck & test..."
npm run typecheck
npm run test
echo "✓ 全部检查通过 (TermFlow)"
