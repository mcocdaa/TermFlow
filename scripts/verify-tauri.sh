#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
MANIFEST="${REPOSITORY_ROOT}/apps/clients/tauri/src-tauri/Cargo.toml"

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Tauri project is not present in this checkout; Rust and unsigned desktop gates were not run." >&2
  exit 0
fi

cargo fmt --manifest-path "${MANIFEST}" --all -- --check
cargo clippy --manifest-path "${MANIFEST}" --all-targets --all-features -- -D warnings
cargo test --manifest-path "${MANIFEST}" --all-targets --all-features
cargo check --manifest-path "${MANIFEST}" --all-targets --all-features
npm run tauri:build --workspace @termflow/tauri-client -- --no-bundle
