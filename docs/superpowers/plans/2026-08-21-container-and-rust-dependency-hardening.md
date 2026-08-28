# Container and Rust Dependency Hardening Implementation Plan

**Implementation status (2026-08-28):** The repository changes in this plan are implemented and pass `scripts/verify.sh`; the release image also passes the real-process health verifier. RustSec still reports 16 explicitly allowed unmaintained-dependency warnings, which remain residual platform risk rather than fixed findings.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Control Plane initialization from changing symlink targets, run its production container with a read-only/capability-minimized profile, and eliminate the known `glib 0.18.5` unsound code path while the Tauri GTK3 dependency cannot yet consume `glib >=0.20`.

**Architecture:** The entrypoint operates on directory entries with no symlink dereference and drops all capabilities before starting the service. Compose supplies only the capabilities required during initialization and makes the image filesystem immutable. For `RUSTSEC-2024-0429`, the exact checksummed crate source is vendored, the two-line upstream fix is backported, Cargo is forced to use that source, and CI permits the advisory ID only after verifying the patched code and an optimized regression test.

**Tech Stack:** POSIX shell, Docker Compose, pytest, Rust/Cargo, cargo-audit, GitHub Actions

---

## Security invariants

- Recursive ownership initialization never dereferences a symlink found inside `/app/data` or `/app/totp-secrets`.
- The service root filesystem is read-only; `/tmp` is a bounded tmpfs; the container has `no-new-privileges`; runtime PID 1 is non-root with an empty effective capability set.
- The root entrypoint receives only `CHOWN`, `DAC_OVERRIDE`, `SETUID`, `SETGID`, and `SETPCAP`, then explicitly drops its bounding, inheritable, ambient, and effective capabilities when switching user.
- Cargo resolves `glib 0.18.5` to the checked-in patched path, not the registry source.
- The `RUSTSEC-2024-0429` audit exception cannot pass if either `let mut p` or `&mut p` is absent, if immutable `&p` returns, or if the optimized iterator regression fails.
- GTK3 “unmaintained” advisories remain a documented platform dependency risk; this plan does not mislabel them as fixed by the `glib` backport.

### Task 1: Stop entrypoint ownership repair from dereferencing symlinks

**Files:**
- Modify: `deploy/entrypoint.control-plane.sh`
- Modify: `scripts/verify-control-plane-image.sh`
- Modify: `tests/deploy/test_control_plane_image_build.py`

- [ ] **Step 1: Add a failing static contract**

In `test_control_plane_image_build.py`, require both ownership commands to use `chown -h` and forbid the two unsafe forms:

```python
entrypoint = Path("deploy/entrypoint.control-plane.sh").read_text(encoding="utf-8")
assert 'chown -h termflow:termflow "${dir}"' in entrypoint
assert 'find "${dir}" -xdev -exec chown -h termflow:termflow {} +' in entrypoint
assert 'chown termflow:termflow "${dir}"' not in entrypoint
assert '-exec chown termflow:termflow {} +' not in entrypoint
```

- [ ] **Step 2: Add an image-level symlink-target regression**

Extend `verify-control-plane-image.sh` after its mount-point symlink test. Create three exact temporary directories, record the sentinel UID, and place links in both initialized trees:

```bash
SYMLINK_DATA_DIR="$(mktemp -d)"
SYMLINK_TOTP_DIR="$(mktemp -d)"
SYMLINK_SENTINEL_DIR="$(mktemp -d)"
touch "${SYMLINK_SENTINEL_DIR}/must-keep-owner"
sentinel_uid="$(stat -c %u "${SYMLINK_SENTINEL_DIR}/must-keep-owner")"
ln -s /sentinel/must-keep-owner "${SYMLINK_DATA_DIR}/data-link"
ln -s /sentinel/must-keep-owner "${SYMLINK_TOTP_DIR}/totp-link"

docker run --rm \
  --volume "${SYMLINK_DATA_DIR}:/app/data" \
  --volume "${SYMLINK_TOTP_DIR}:/app/totp-secrets" \
  --volume "${SYMLINK_SENTINEL_DIR}:/sentinel" \
  "${CONTROL_PLANE_IMAGE}" true

test "$(stat -c %u "${SYMLINK_SENTINEL_DIR}/must-keep-owner")" = "${sentinel_uid}"
```

Add these directories to the existing exact cleanup function. Do not use a wildcard or delete a parent directory.

- [ ] **Step 3: Run the static test and image verifier to demonstrate the defect**

```bash
.envs/dev/bin/python -m pytest tests/deploy/test_control_plane_image_build.py -q
scripts/build-control-plane-image.sh termflow-control-plane:symlink-audit
scripts/verify-control-plane-image.sh termflow-control-plane:symlink-audit
```

Expected before the fix: the static contract fails; the sentinel test either changes ownership or causes the unsafe entrypoint path to fail.

- [ ] **Step 4: Apply no-dereference ownership changes**

Change the two entrypoint lines to:

```sh
chown -h termflow:termflow "${dir}"
find "${dir}" -xdev -exec chown -h termflow:termflow {} +
```

Keep the mount-point `-L` rejection and `find -xdev` boundary.

- [ ] **Step 5: Rebuild, verify, and commit**

```bash
.envs/dev/bin/python -m pytest tests/deploy/test_control_plane_image_build.py -q
scripts/build-control-plane-image.sh termflow-control-plane:symlink-audit
scripts/verify-control-plane-image.sh termflow-control-plane:symlink-audit
git add -- deploy/entrypoint.control-plane.sh \
  scripts/verify-control-plane-image.sh tests/deploy/test_control_plane_image_build.py
git commit -m "security: prevent entrypoint symlink ownership traversal"
```

### Task 2: Enforce a read-only, capability-minimized container profile

**Files:**
- Modify: `deploy/entrypoint.control-plane.sh`
- Modify: `deploy/compose.yaml`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `scripts/verify-control-plane-image.sh`
- Modify: `docs/operations.md`

- [ ] **Step 1: Add failing Compose contracts**

Parse the rendered Compose service and assert:

```python
assert service["read_only"] is True
assert service["cap_drop"] == ["ALL"]
assert set(service["cap_add"]) == {
    "CHOWN",
    "DAC_OVERRIDE",
    "SETUID",
    "SETGID",
    "SETPCAP",
}
assert service["security_opt"] == ["no-new-privileges:true"]
assert "/tmp:size=64m,mode=1777" in service["tmpfs"]
```

In the entrypoint contract, require `--bounding-set=-all`, `--inh-caps=-all`, and `--ambient-caps=-all` on the final `setpriv` execution.

- [ ] **Step 2: Run the contracts and observe failures**

```bash
TERMFLOW_ADMIN_TOKEN=contract-admin-token-that-is-long-enough \
  .envs/dev/bin/python -m pytest tests/deploy/test_compose_contract.py -q
```

- [ ] **Step 3: Add the production Compose profile**

Under `services.control-plane`, add:

```yaml
read_only: true
tmpfs:
  - /tmp:size=64m,mode=1777
cap_drop:
  - ALL
cap_add:
  - CHOWN
  - DAC_OVERRIDE
  - SETUID
  - SETGID
  - SETPCAP
security_opt:
  - no-new-privileges:true
```

The two named volumes remain the only persistent writable paths.

- [ ] **Step 4: Make the UID transition discard all remaining capabilities**

Change the root branch to:

```sh
exec setpriv \
    --reuid termflow \
    --regid termflow \
    --clear-groups \
    --bounding-set=-all \
    --inh-caps=-all \
    --ambient-caps=-all \
    -- "$@"
```

- [ ] **Step 5: Apply the same profile to image acceptance**

Define this array in `verify-control-plane-image.sh`:

```bash
CONTROL_PLANE_RUNTIME_SECURITY_ARGS=(
  --read-only
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
  --cap-drop ALL
  --cap-add CHOWN
  --cap-add DAC_OVERRIDE
  --cap-add SETUID
  --cap-add SETGID
  --cap-add SETPCAP
  --security-opt no-new-privileges:true
)
```

Pass it to the bind-mount initialization test, symlink-target test, and detached
health-check container. For the normal PID-1 test, also mount writable tmpfs at
`/app/data` and `/app/totp-secrets`; a read-only root filesystem without the two
production volumes is intentionally not bootable. Keep the mount-point symlink
rejection test outside the read-only profile because it deliberately rewrites
the image path before calling the entrypoint. Inside the detached container,
assert:

```sh
test "$(awk '/^CapEff:/ { print $2 }' /proc/1/status)" = "0000000000000000"
touch /app/rootfs-write-probe 2>/dev/null && exit 1 || true
touch /tmp/tmpfs-write-probe
```

- [ ] **Step 6: Render, build, and run acceptance**

```bash
TERMFLOW_ADMIN_TOKEN=contract-admin-token-that-is-long-enough \
  docker compose -f deploy/compose.yaml config --quiet
scripts/build-control-plane-image.sh termflow-control-plane:hardened
scripts/verify-control-plane-image.sh termflow-control-plane:hardened
```

Expected: initialization succeeds, health responds, PID 1 has zero effective capabilities, rootfs write fails, and `/tmp` write succeeds.

- [ ] **Step 7: Document and commit**

Document the required capabilities: `DAC_OVERRIDE` is needed only while
traversing fresh root/user-owned bind mounts, and `SETPCAP` is needed only so
`setpriv` can delete the temporary initialization capabilities from its own
bounding/inheritable/ambient sets before exec. Commit:

```bash
git add -- deploy/entrypoint.control-plane.sh deploy/compose.yaml \
  scripts/verify-control-plane-image.sh tests/deploy/test_compose_contract.py \
  docs/operations.md
git commit -m "security: minimize Control Plane container privileges"
```

### Task 3: Vendor and verify the upstream `glib` unsoundness fix

**Files:**
- Create: `vendor/glib-0.18.5/` from the exact crates.io archive
- Modify: `vendor/glib-0.18.5/src/variant_iter.rs`
- Create: `vendor/glib-0.18.5/TERMFLOW-PATCH.md`
- Modify: `apps/clients/tauri/src-tauri/Cargo.toml`
- Modify: `apps/clients/tauri/src-tauri/Cargo.lock`
- Create: `tests/security/test_rust_dependency_patch.py`

- [ ] **Step 1: Add a failing repository patch contract**

Create `tests/security/test_rust_dependency_patch.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHED_SOURCE = ROOT / "vendor/glib-0.18.5/src/variant_iter.rs"


def test_glib_variant_string_iterator_uses_mutable_out_pointer() -> None:
    source = PATCHED_SOURCE.read_text(encoding="utf-8")
    assert "let mut p: *mut libc::c_char = std::ptr::null_mut();" in source
    assert "                &mut p," in source
    assert "                &p," not in source


def test_tauri_cargo_uses_the_patched_glib_source() -> None:
    cargo = (ROOT / "apps/clients/tauri/src-tauri/Cargo.toml").read_text(
        encoding="utf-8"
    )
    assert '[patch.crates-io]' in cargo
    assert 'glib = { path = "../../../../vendor/glib-0.18.5" }' in cargo
```

- [ ] **Step 2: Run the contract and observe missing-vendor failure**

```bash
.envs/dev/bin/python -m pytest tests/security/test_rust_dependency_patch.py -q
```

- [ ] **Step 3: Fetch and verify the exact locked crate source**

Use the checksum already recorded in `Cargo.lock`:

```bash
archive="$(mktemp)"
curl --fail --location --proto '=https' --tlsv1.2 \
  https://crates.io/api/v1/crates/glib/0.18.5/download \
  --output "$archive"
echo "233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5  $archive" \
  | sha256sum --check --strict
mkdir -p vendor
tar -xzf "$archive" -C vendor
rm -f "$archive"
```

Confirm the vendored crate contains its original `LICENSE`, `COPYRIGHT`,
`Cargo.toml`, and `.cargo-checksum.json` before editing.

- [ ] **Step 4: Apply the exact upstream correction**

In `vendor/glib-0.18.5/src/variant_iter.rs`, change only:

```rust
let mut p: *mut libc::c_char = std::ptr::null_mut();
```

and the variadic out argument to:

```rust
&mut p,
```

This is the correction described by [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html) and gtk-rs-core PR 1343. Do not modify the crate version.

Create `TERMFLOW-PATCH.md` recording: advisory ID, original crate checksum, the two changed lines, upstream PR URL, date applied, and the removal condition `the resolved GTK/Tauri graph accepts glib >=0.20`.

Delete `vendor/glib-0.18.5/.cargo-checksum.json` after applying the patch because
its per-file hashes describe the unmodified crates.io archive and Cargo treats
this directory as a path dependency. Preserve the original archive checksum in
`TERMFLOW-PATCH.md`.

- [ ] **Step 5: Force the Tauri graph onto the patched path**

Append to the application `Cargo.toml`:

```toml
[patch.crates-io]
glib = { path = "../../../../vendor/glib-0.18.5" }
```

Refresh only that lock entry offline and inspect the inverse tree:

```bash
cargo update --manifest-path apps/clients/tauri/src-tauri/Cargo.toml \
  -p glib@0.18.5 --offline
cargo tree --manifest-path apps/clients/tauri/src-tauri/Cargo.toml \
  -i glib@0.18.5
```

Expected: the tree labels `glib v0.18.5 (/.../TermFlow/vendor/glib-0.18.5)` and every GTK/Tauri consumer reaches that same patched node.

- [ ] **Step 6: Run the optimized upstream iterator regression**

```bash
cargo test --manifest-path vendor/glib-0.18.5/Cargo.toml \
  --release variant_iter::tests::test_variant_iter_array -- --exact
cargo test --manifest-path vendor/glib-0.18.5/Cargo.toml \
  --release test_variant_str_iter_nth
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --release
.envs/dev/bin/python -m pytest tests/security/test_rust_dependency_patch.py -q
```

Expected: the optimized string iterator tests and TermFlow Rust tests pass.

- [ ] **Step 7: Commit the vendored correction separately**

```bash
git add -- vendor/glib-0.18.5 \
  apps/clients/tauri/src-tauri/Cargo.toml \
  apps/clients/tauri/src-tauri/Cargo.lock \
  tests/security/test_rust_dependency_patch.py
git commit -m "security: backport the glib VariantStrIter fix"
```

### Task 4: Gate RustSec audits without hiding an unpatched advisory

**Files:**
- Create: `scripts/security/verify-rust-dependencies.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/verify.sh`
- Modify: `docs/security.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Implement a patch-aware audit script**

Create this executable script:

```bash
#!/usr/bin/env bash
set -euo pipefail

manifest="apps/clients/tauri/src-tauri/Cargo.toml"
patch_source="vendor/glib-0.18.5/src/variant_iter.rs"

grep -Fq 'let mut p: *mut libc::c_char = std::ptr::null_mut();' "$patch_source"
grep -Fq '                &mut p,' "$patch_source"
if grep -Fq '                &p,' "$patch_source"; then
  echo "RUSTSEC-2024-0429 backport is missing" >&2
  exit 1
fi

cargo test --manifest-path vendor/glib-0.18.5/Cargo.toml \
  --release variant_iter::tests::test_variant_iter_array -- --exact
cargo tree --manifest-path "$manifest" -i glib@0.18.5 \
  | grep -Fq 'vendor/glib-0.18.5'
cargo audit \
  --file apps/clients/tauri/src-tauri/Cargo.lock \
  --ignore RUSTSEC-2024-0429
```

The ignore is acceptable only because the three preceding checks prove the local patched source is active.

- [ ] **Step 2: Wire the audit into local and CI verification**

Add the script to `scripts/verify.sh`. In the CI install step, pin the current audited tool release:

```bash
cargo install cargo-audit --locked --version 0.22.2
```

Then add a named `Verify Rust dependency advisories` step that runs `scripts/security/verify-rust-dependencies.sh`.

- [ ] **Step 3: Document residual warnings and the removal gate**

In `docs/security.md`, record:

- `RUSTSEC-2024-0429` is locally patched but still reported by version-only scanners; the checked-in exception is patch-gated.
- GTK3 unmaintained advisories are supply-chain/maintenance warnings inherited through Linux Tauri/Wry; they are not memory-safety fixes and remain residual risk.
- The vendor directory and ignore must be removed in the same change once `cargo tree -i glib` resolves a maintained `glib >=0.20` for all Linux WebView consumers.

Add documentation contract assertions for the advisory ID, `vendor/glib-0.18.5`, and the removal condition.

- [ ] **Step 4: Run all security and repository gates**

```bash
chmod 0755 scripts/security/verify-rust-dependencies.sh
bash -n scripts/security/verify-rust-dependencies.sh
cargo install cargo-audit --locked --version 0.22.2
scripts/security/verify-rust-dependencies.sh
.envs/dev/bin/python -m pytest \
  tests/security/test_rust_dependency_patch.py \
  tests/docs/test_documentation_contract.py -q
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH ./scripts/verify.sh
```

Expected: the patch-aware audit passes; informational GTK3 maintenance warnings remain visible in audit output; the full repository verifier passes.

- [ ] **Step 5: Commit audit automation and documentation**

```bash
git add -- scripts/security/verify-rust-dependencies.sh scripts/verify.sh \
  .github/workflows/ci.yml docs/security.md \
  tests/docs/test_documentation_contract.py
git commit -m "ci: enforce patch-aware Rust dependency audits"
```

### Task 5: Final image and release evidence

**Files:**
- Verify only: hardened image, Rust/Tauri builds, CI

- [ ] **Step 1: Rebuild from the final tree and rerun the hardened image verifier**

```bash
scripts/build-control-plane-image.sh termflow-control-plane:security-final
scripts/verify-control-plane-image.sh termflow-control-plane:security-final
scripts/release/verify_control_plane_release_image.sh termflow-control-plane:security-final
```

- [ ] **Step 2: Verify unsigned desktop builds on Linux, Windows, and macOS CI**

The vendored patch is in the shared lock graph but the vulnerable GTK path is Linux-specific. Require all three `tauri-desktop-unsigned` matrix jobs to pass so the patch does not break non-Linux resolution.

- [ ] **Step 3: Record what is and is not proven**

Capture image digest, final commit SHA, `cargo tree -i glib@0.18.5`, audit output, optimized iterator output, and CI run URL. State separately that runtime image restrictions were container-tested and that GTK3 maintenance status remains residual risk; do not claim absolute security.
