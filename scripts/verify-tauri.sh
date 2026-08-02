#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
MANIFEST="${REPOSITORY_ROOT}/apps/clients/tauri/src-tauri/Cargo.toml"

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Tauri project is required; Rust and unsigned desktop gates cannot be skipped." >&2
  exit 1
fi

has_linux_prerequisites() {
  command -v pkg-config >/dev/null 2>&1 \
    && pkg-config --exists webkit2gtk-4.1 gtk+-3.0 javascriptcoregtk-4.1 libsoup-3.0
}

run_rust_gates() {
  cargo fmt --manifest-path "${MANIFEST}" --all -- --check
  cargo clippy --manifest-path "${MANIFEST}" --all-targets --all-features -- -D warnings
  cargo test --manifest-path "${MANIFEST}" --all-targets --all-features
  cargo check --manifest-path "${MANIFEST}" --all-targets --all-features
}

if has_linux_prerequisites; then
  run_rust_gates
  npm run tauri:build --workspace @termflow/tauri-client -- --no-bundle
  exit 0
fi

# The repository's verification host may not have GTK/WebKit development
# packages. Keep the gate fail-closed while providing a reproducible Linux
# fallback; the container runs only Rust checks and never enters a delivery
# image. The Vite/Tauri WebView build still runs on the pinned host Node.
if [[ "${TERMFLOW_TAURI_DOCKER_FALLBACK:-1}" != "1" ]] || ! command -v docker >/dev/null 2>&1; then
  echo "Tauri Linux prerequisites are missing; install GTK/WebKit packages or enable Docker fallback." >&2
  exit 1
fi

npm run build --workspace @termflow/tauri-client
image="${TERMFLOW_TAURI_CI_IMAGE:-termflow-tauri-ci:rust-1.97.1}"
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  docker build -f "${REPOSITORY_ROOT}/deploy/Dockerfile.tauri-linux-ci" -t "${image}" "${REPOSITORY_ROOT}"
fi
docker run --rm \
  -v "${REPOSITORY_ROOT}:/source:ro" \
  -v "${TERMFLOW_TAURI_TARGET_VOLUME:-termflow-tauri-target}:/target" \
  -e CARGO_TARGET_DIR=/target \
  "${image}" \
  bash -c 'cp -a /source /tmp/termflow-tauri && cd /tmp/termflow-tauri/apps/clients/tauri/src-tauri && cargo fmt --all -- --check && cargo clippy --all-targets --all-features -- -D warnings && cargo test --all-targets --all-features && cargo check --all-targets --all-features'
