# TermFlow Mobile Keyring Build Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the iOS CI build and make both iOS and Android register a real native credential store before TermFlow reads or writes device keys and refresh tokens.

**Architecture:** Keep the `keyring` v1 convenience facade only on desktop targets where it initializes macOS Keychain, Windows Credential Manager, and Linux Secret Service. Mobile targets depend directly on `keyring-core` plus the platform store, enable Apple `protected` on iOS, and initialize the store once before creating entries. This fixes both the observed compile failure and the otherwise latent mobile `NoDefaultStore` runtime failure.

**Tech Stack:** Rust 2021, Cargo target-specific dependencies/features, keyring-core 1.0.0, apple-native-keyring-store 1.0.1, android-native-keyring-store 1.0.0, Tauri 2 iOS/Android CI.

---

## Root-cause evidence

- `Cargo.toml` declares `keyring = 4.1.5` with its default `v1` feature plus Android store support.
- `cargo tree --target aarch64-apple-ios-sim -e features -i apple-native-keyring-store` shows `keychain` but no `protected`.
- `apple-native-keyring-store 1.0.1/src/lib.rs` deliberately emits `compile_error!` for iOS without `protected`.
- `keyring 4.1.5/src/v1.rs` initializes stores only for macOS, Windows, and desktop Unix; iOS/Android reach `Entry::new` without a registered default store.
- Therefore adding `protected` alone addresses only compilation. Correct mobile behavior also requires explicit store registration.

## File responsibility map

- `apps/clients/tauri/src-tauri/Cargo.toml`: desktop/mobile dependency and feature selection.
- `apps/clients/tauri/src-tauri/Cargo.lock`: locked direct store dependency graph.
- `apps/clients/tauri/src-tauri/src/auth.rs`: once-only mobile store registration and common entry/error aliases.
- `tests/tauri/test_mobile_keyring_contract.py`: target dependency and feature regression contract.
- `.github/workflows/ci.yml`: existing real iOS/Android compile gates; no job is disabled or made optional.

### Task 1: Add a failing mobile keyring target contract

**Files:**
- Create: `tests/tauri/test_mobile_keyring_contract.py`

- [ ] **Step 1: Write the dependency/source contract**

Use `tomllib` and source assertions to require:

```python
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "apps/clients/tauri/src-tauri/Cargo.toml"


def test_mobile_targets_use_explicit_native_stores() -> None:
    manifest = tomllib.loads(MANIFEST.read_text())
    dependencies = manifest["dependencies"]
    assert "keyring" not in dependencies

    targets = manifest["target"]
    desktop = targets['cfg(not(any(target_os = "ios", target_os = "android")))'][
        "dependencies"
    ]
    ios = targets['cfg(target_os = "ios")']["dependencies"]
    android = targets['cfg(target_os = "android")']["dependencies"]

    assert desktop["keyring"]["version"] == "=4.1.5"
    assert ios["keyring-core"]["version"] == "=1.0.0"
    assert ios["apple-native-keyring-store"]["features"] == ["protected"]
    assert android["keyring-core"]["version"] == "=1.0.0"
    assert android["android-native-keyring-store"]["version"] == "=1.0.0"


def test_ios_feature_tree_enables_protected_store() -> None:
    result = subprocess.run(
        [
            "cargo",
            "tree",
            "--manifest-path",
            str(MANIFEST),
            "--target",
            "aarch64-apple-ios-sim",
            "-e",
            "features",
            "-i",
            "apple-native-keyring-store",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'apple-native-keyring-store feature "protected"' in result.stdout


def test_mobile_store_is_initialized_before_entry_creation() -> None:
    source = (ROOT / "apps/clients/tauri/src-tauri/src/auth.rs").read_text()
    assert "fn initialize_mobile_keyring" in source
    assert "initialize_mobile_keyring()?;" in source
    assert source.index("initialize_mobile_keyring()?;") < source.index(
        "KeyringEntry::new(KEYRING_SERVICE"
    )
```

- [ ] **Step 2: Run the contract and verify all three root-cause checks fail**

```bash
uv run pytest tests/tauri/test_mobile_keyring_contract.py -q
```

Expected: dependency layout assertion fails, feature-tree output lacks `protected`, and initialization source is absent.

- [ ] **Step 3: Commit the failing regression test**

Do not commit a permanently red branch. Keep the failing test in the worktree, record the expected failures in the execution notes, and proceed directly to Task 2.

### Task 2: Use and initialize the correct native stores on mobile

**Files:**
- Modify: `apps/clients/tauri/src-tauri/Cargo.toml`
- Modify: `apps/clients/tauri/src-tauri/Cargo.lock`
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`

- [ ] **Step 1: Move the v1 facade to desktop and add explicit mobile stores**

Remove `keyring` from `[dependencies]` and add these exact target sections:

```toml
[target.'cfg(not(any(target_os = "ios", target_os = "android")))'.dependencies]
keyring = { version = "=4.1.5", features = [] }

[target.'cfg(target_os = "ios")'.dependencies]
keyring-core = "=1.0.0"
apple-native-keyring-store = { version = "=1.0.1", features = ["protected"] }

[target.'cfg(target_os = "android")'.dependencies]
keyring-core = "=1.0.0"
android-native-keyring-store = "=1.0.0"
```

Retain the existing desktop-only `tauri-plugin-single-instance` section. Run `cargo update --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --workspace` only if Cargo requires lock normalization; do not upgrade unrelated locked crates.

- [ ] **Step 2: Add common aliases and once-only mobile initialization**

Use conditional aliases:

```rust
#[cfg(not(any(target_os = "ios", target_os = "android")))]
use keyring::{Entry as KeyringEntry, Error as KeyringError};
#[cfg(any(target_os = "ios", target_os = "android"))]
use keyring_core::{Entry as KeyringEntry, Error as KeyringError};
#[cfg(any(target_os = "ios", target_os = "android"))]
use std::sync::OnceLock;
```

Add one initializer whose stored error contains only the existing safe product code:

```rust
#[cfg(any(target_os = "ios", target_os = "android"))]
fn initialize_mobile_keyring() -> Result<(), String> {
    static INITIALIZED: OnceLock<Result<(), String>> = OnceLock::new();
    INITIALIZED
        .get_or_init(|| {
            #[cfg(target_os = "ios")]
            let store = apple_native_keyring_store::protected::Store::new();
            #[cfg(target_os = "android")]
            let store = android_native_keyring_store::Store::new();

            store
                .map(keyring_core::set_default_store)
                .map_err(|_| safe_error("secure_store_unavailable"))
        })
        .clone()
}

#[cfg(not(any(target_os = "ios", target_os = "android")))]
fn initialize_mobile_keyring() -> Result<(), String> {
    Ok(())
}
```

Call it before every common entry construction:

```rust
fn keyring_entry(issuer: &str, kind: &str) -> Result<KeyringEntry, String> {
    initialize_mobile_keyring()?;
    KeyringEntry::new(KEYRING_SERVICE, &issuer_key(issuer, kind))
        .map_err(|_| safe_error("secure_store_unavailable"))
}
```

Replace `keyring::Error::NoEntry` matches with `KeyringError::NoEntry`. Do not return underlying platform errors, access-group names, credentials, or key material.

- [ ] **Step 3: Regenerate only the Cargo lock changes required by the manifest**

```bash
cargo metadata --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --format-version 1 --locked > /dev/null
```

If `--locked` reports stale lock data, run:

```bash
cargo update --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --workspace
```

Then inspect `git diff -- apps/clients/tauri/src-tauri/Cargo.lock` and reject unrelated version upgrades.

- [ ] **Step 4: Run the regression contract and desktop Rust gates**

```bash
uv run pytest tests/tauri/test_mobile_keyring_contract.py -q
scripts/verify-tauri.sh
```

Expected: contract PASS; iOS feature tree contains `protected`; Rust fmt/clippy/test/check and Linux `--no-bundle` compile exit 0.

- [ ] **Step 5: Verify Android feature selection**

```bash
cargo tree --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --target aarch64-linux-android -e features -i android-native-keyring-store
```

Expected: the Android store is reached directly from `termflow-client`, not only through the desktop v1 facade.

- [ ] **Step 6: Commit the mobile keyring fix**

```bash
git add apps/clients/tauri/src-tauri/Cargo.toml apps/clients/tauri/src-tauri/Cargo.lock apps/clients/tauri/src-tauri/src/auth.rs tests/tauri/test_mobile_keyring_contract.py
git commit -m "fix(tauri): initialize protected mobile keyrings"
```

### Task 3: Re-run real iOS and Android build gates

**Files:**
- Modify: `.github/workflows/ci.yml` only if a diagnostic assertion is needed; do not remove, skip, or allow-failure either mobile job.

- [ ] **Step 1: Push the fix commit and run existing CI**

Require `tauri-ios-unsigned` to execute the same `tauri ios build --debug --ci --target aarch64-sim` command from the reported failure. Require `tauri-android-unsigned` to build the aarch64 debug APK.

- [ ] **Step 2: Inspect iOS dependency and Xcode output**

Expected: `apple-native-keyring-store` compiles with `protected`; the previous compile error is absent; the Xcode `Build Rust Code` phase and overall iOS job exit 0. A feature-tree test alone is not completion evidence.

- [ ] **Step 3: Inspect Android output**

Expected: direct `android-native-keyring-store` compiles, Tauri produces the debug APK, and no `NoDefaultStore` compile-time fallback or sample/in-memory credential store is introduced.

- [ ] **Step 4: Run the full local verification after CI configuration remains intact**

```bash
scripts/verify.sh
```

Expected: all local repository gates exit 0 and `.github/workflows/ci.yml` still contains required non-optional iOS and Android jobs.
