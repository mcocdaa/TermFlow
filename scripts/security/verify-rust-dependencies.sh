#!/usr/bin/env bash
set -euo pipefail

manifest="apps/clients/tauri/src-tauri/Cargo.toml"
patch_source="vendor/glib-0.18.5/src/variant_iter.rs"

grep -Fq 'let mut p: *mut libc::c_char = std::ptr::null_mut();' "${patch_source}"
grep -Fq '                &mut p,' "${patch_source}"
if grep -Fq '                &p,' "${patch_source}"; then
  echo "RUSTSEC-2024-0429 backport is missing" >&2
  exit 1
fi

cargo test --manifest-path vendor/glib-0.18.5/Cargo.toml \
  --release variant_iter::tests::test_variant_iter_array -- --exact
cargo test --manifest-path vendor/glib-0.18.5/Cargo.toml \
  --release test_variant_str_iter_nth

resolved_tree="$(cargo tree --manifest-path "${manifest}" -i glib@0.18.5)"
if ! grep -Fq 'vendor/glib-0.18.5' <<<"${resolved_tree}"; then
  echo "Tauri is not resolving glib 0.18.5 from the patched vendor source" >&2
  exit 1
fi

audit_db_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "${audit_db_dir}"
}
trap cleanup EXIT

curl --fail --location --silent --show-error \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://codeload.github.com/RustSec/advisory-db/tar.gz/refs/heads/main \
  --output "${audit_db_dir}/advisory-db.tar.gz"
tar -xzf "${audit_db_dir}/advisory-db.tar.gz" -C "${audit_db_dir}"
test -d "${audit_db_dir}/advisory-db-main/crates"

cargo audit \
  --db "${audit_db_dir}/advisory-db-main" \
  --no-fetch \
  --file apps/clients/tauri/src-tauri/Cargo.lock \
  --ignore RUSTSEC-2024-0429
