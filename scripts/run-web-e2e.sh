#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=$(mktemp -d /tmp/termflow-web-e2e.XXXXXX)
CONTROL_PID=""
TERM_ID=""
EXIT_CODE=0

cleanup() {
  EXIT_CODE=$?
  trap - EXIT INT TERM
  if [[ -n "$TERM_ID" ]]; then
    "$REPO_ROOT/.venv/bin/termflow" kill "$TERM_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$CONTROL_PID" ]] && kill -0 "$CONTROL_PID" 2>/dev/null; then
    kill "$CONTROL_PID" 2>/dev/null || true
    wait "$CONTROL_PID" 2>/dev/null || true
  fi
  if [[ "$EXIT_CODE" -eq 0 && "${TERMFLOW_E2E_KEEP:-0}" != "1" ]]; then
    case "$RUN_DIR" in
      /tmp/termflow-web-e2e.*) rm -rf "$RUN_DIR" ;;
      *) echo "Refusing to remove unexpected browser run directory: $RUN_DIR" >&2 ;;
    esac
  else
    echo "TermFlow browser evidence: $RUN_DIR" >&2
  fi
  exit "$EXIT_CODE"
}
trap cleanup EXIT INT TERM

PORT=$(
  "$REPO_ROOT/.venv/bin/python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
)
export TERMFLOW_E2E_BASE_URL="http://127.0.0.1:$PORT"
export TERMFLOW_E2E_ADMIN_TOKEN="browser-e2e-admin-token-that-is-disposable"
export TERMFLOW_E2E_TERM_NAME="resume-terminal"
export TERMFLOW_E2E_ARTIFACT_DIR="$RUN_DIR/playwright"
export TERMFLOW_E2E_SCREENSHOT_DIR="$RUN_DIR/screenshots"
export TERMFLOW_ADMIN_TOKEN="$TERMFLOW_E2E_ADMIN_TOKEN"
export TERMFLOW_DATABASE_URL="sqlite+aiosqlite:///$RUN_DIR/control-plane.db"
export TERMFLOW_ALLOW_INSECURE_LOOPBACK=true
export TERMFLOW_PUBLIC_BASE_URL="$TERMFLOW_E2E_BASE_URL"
export TERMFLOW_TRUSTED_WEB_ORIGINS="$TERMFLOW_E2E_BASE_URL"
export TERMFLOW_STATIC_DIR="$REPO_ROOT/apps/clients/web/dist"
export TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE="$RUN_DIR/totp-master-key"
# The disposable browser run logs in once per project and trajectory. Keep the
# production limiter defaults unchanged while avoiding cross-test exhaustion.
export TERMFLOW_AUTH_ATTEMPT_BUDGET_CAPACITY=100
export XDG_CONFIG_HOME="$RUN_DIR/config"
export XDG_STATE_HOME="$RUN_DIR/state"
export XDG_RUNTIME_DIR="$RUN_DIR/runtime"
mkdir -m 700 -p "$XDG_RUNTIME_DIR" "$TERMFLOW_E2E_SCREENSHOT_DIR"

(
  cd "$REPO_ROOT"
  npm run build:web
)
"$REPO_ROOT/.venv/bin/termflow-control" serve --host 127.0.0.1 --port "$PORT" \
  >"$RUN_DIR/control-plane.log" 2>&1 &
CONTROL_PID=$!

for _ in $(seq 1 100); do
  if curl -fsS "$TERMFLOW_E2E_BASE_URL/healthz" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$CONTROL_PID" 2>/dev/null; then
    sed -n '1,240p' "$RUN_DIR/control-plane.log" >&2
    exit 1
  fi
  sleep 0.05
done
curl -fsS "$TERMFLOW_E2E_BASE_URL/healthz" >/dev/null

FIXTURE_JSON=$("$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/web_e2e_fixture.py")
TERM_ID=$(
  "$REPO_ROOT/.venv/bin/python" -c \
    'import json,sys; print(json.load(sys.stdin)["online_term_id"])' <<<"$FIXTURE_JSON"
)
export TERMFLOW_E2E_TERM_ID="$TERM_ID"
TERMFLOW_E2E_OFFLINE_TERM_IDS=$(
  "$REPO_ROOT/.venv/bin/python" -c \
    'import json,sys; print(json.dumps(json.load(sys.stdin)["offline_term_ids"], separators=(",", ":")))' \
    <<<"$FIXTURE_JSON"
)
export TERMFLOW_E2E_OFFLINE_TERM_IDS

for _ in $(seq 1 100); do
  if curl -fsS -H "Authorization: Bearer $TERMFLOW_E2E_ADMIN_TOKEN" \
    "$TERMFLOW_E2E_BASE_URL/api/v1/instances" | \
    "$REPO_ROOT/.venv/bin/python" -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if any(item["online"] for item in data["instances"]) else 1)'; then
    break
  fi
  sleep 0.05
done

(
  cd "$REPO_ROOT/apps/clients/web"
  npm run e2e -- "$@"
)
