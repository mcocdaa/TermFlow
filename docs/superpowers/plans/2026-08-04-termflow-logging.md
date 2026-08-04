# TermFlow Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, rotating, platform-standard diagnostic logs to A and Tauri C, preserve B's Docker stdout/stderr logging, and document how Web C request IDs connect browser failures to B logs.

**Architecture:** A gets a small Python logging module backed by `platformdirs.user_log_path("termflow")` and a rotating JSONL file handler. Tauri C gets a Rust-owned logger initialized from Tauri's `app_log_dir()`, exposed through a narrow command for sanitized TypeScript events; the native HTTP transport records response status and `X-Request-ID` without query secrets. B remains stdout/stderr-only, while Web C keeps its existing no-persistence boundary and relies on B request IDs.

**Tech Stack:** Python `logging.handlers.RotatingFileHandler`, `platformdirs`, Tauri 2 path resolver, Rust `std::fs`/`serde_json`, Vue/TypeScript runtime adapters, Vitest, Pytest, Rust unit tests.

---

### Task 1: Add the A logging module and path contract

**Files:**
- Create: `apps/node/src/termflow_node/logging.py`
- Create: `apps/node/tests/test_logging.py`
- Modify: `apps/node/src/termflow_node/cli.py:24-37`

- [ ] **Step 1: Write failing path, JSON, rotation, and redaction tests**

  Test an injected log directory, assert the path is created with private permissions where supported, assert one JSON object per line with UTC timestamp/component/level/event, assert 10 MiB rotation keeps five backups, and assert values containing `token`, `secret`, `cookie`, `authorization`, or terminal text are replaced or omitted.

- [ ] **Step 2: Run the focused tests and confirm they fail**

  Run: `uv run --package termflow-node pytest apps/node/tests/test_logging.py -q`

  Expected: import or symbol failures because the logging module does not exist.

- [ ] **Step 3: Implement the minimal logger**

  Add `log_path()`, `configure_logging()`, and `log_event()` using `platformdirs.user_log_path("termflow")`, a `RotatingFileHandler(maxBytes=10 * 1024 * 1024, backupCount=5)`, UTC RFC 3339 timestamps, JSON encoding, and an allowlisted metadata payload. Make handler setup idempotent and make logging failures non-fatal.

- [ ] **Step 4: Configure the CLI and Bridge entrypoint**

  Call `configure_logging()` from the Typer callback before command dispatch so normal CLI commands and `_bridge` share the same log root. Add sanitized lifecycle events around login, instance creation/activation, and bridge startup/failure without writing credentials or terminal data.

- [ ] **Step 5: Run the focused tests and the existing A suite**

  Run: `uv run --package termflow-node pytest apps/node/tests/test_logging.py apps/node/tests/test_instance_store.py apps/node/tests/test_cli_lifecycle.py -q`

  Expected: all focused tests pass.

- [ ] **Step 6: Commit the A logging slice**

  ```bash
  git add apps/node/src/termflow_node/logging.py apps/node/tests/test_logging.py apps/node/src/termflow_node/cli.py
  git commit -m "feat(node): add platform log files"
  ```

### Task 2: Add the Tauri C native logger

**Files:**
- Create: `apps/clients/tauri/src-tauri/src/diagnostics.rs`
- Create: `apps/clients/tauri/src/diagnostics.ts`
- Create: `apps/clients/tauri/src/diagnostics.test.ts`
- Modify: `apps/clients/tauri/src-tauri/src/lib.rs:1-40`
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs:1-40,250-450`

- [ ] **Step 1: Write failing Rust logger tests**

  Test an injected temporary log directory, JSONL output, 10 MiB/5-backup rotation behavior, and that the serialized record cannot contain a supplied credential-like field or value.

- [ ] **Step 2: Run the Rust tests and confirm they fail**

  Run: `cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml diagnostics::tests --lib`

  Expected: module/type failures because `diagnostics.rs` is not present.

- [ ] **Step 3: Implement the Rust logger and Tauri command**

  Add a managed `NativeLogger` initialized during Tauri setup from `app.path().app_log_dir()`. Write `termflow-client.log` as JSONL, rotate at 10 MiB with five backups, use UTC timestamps, and expose `native_log(event, level, issuer, request_id, error_code)` with strict field length/character validation. Never accept arbitrary message text, URLs, tokens, or secret payloads from the frontend.

- [ ] **Step 4: Add the TypeScript diagnostics adapter and tests**

  Wrap `invoke('native_log')` in a fire-and-forget adapter that never changes application behavior when logging fails. Test event mapping and that the adapter sends only origin/request ID/error code metadata.

- [ ] **Step 5: Run Tauri unit and Rust tests**

  Run: `npm run test:run --workspace @termflow/tauri-client -- src/diagnostics.test.ts src/views/NativeConnectView.test.ts src/adapters/tauriHttpTransport.test.ts`

  Run: `cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --lib`

  Expected: all tests pass without logging secrets.

- [ ] **Step 6: Commit the native logger slice**

  ```bash
  git add apps/clients/tauri/src-tauri/src/diagnostics.rs apps/clients/tauri/src/diagnostics.ts apps/clients/tauri/src/diagnostics.test.ts apps/clients/tauri/src-tauri/src/lib.rs apps/clients/tauri/src-tauri/src/auth.rs
  git commit -m "feat(tauri): add rotating native diagnostics"
  ```

### Task 3: Instrument native authorization and HTTP request correlation

**Files:**
- Modify: `apps/clients/tauri/src/nativeAuth.ts:1-60`
- Modify: `apps/clients/tauri/src/adapters/tauriAuthorization.ts:1-60`
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.ts:1-80`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue:22-62`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.test.ts`
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts`

- [ ] **Step 1: Add failing authorization-stage assertions**

  Assert that connect emits `connect_started`, metadata success/failure, browser-open success/failure, callback received/invalid, and token exchange success/failure. Assert errors expose only stable codes and never raw URL query or credential data.

- [ ] **Step 2: Run the focused tests and confirm the new assertions fail**

  Run: `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts src/adapters/tauriAuthorization.test.ts src/adapters/tauriHttpTransport.test.ts`

  Expected: event assertions fail because no diagnostics calls exist.

- [ ] **Step 3: Instrument the flow with sanitized events**

  Log only the canonical issuer origin, path-safe request metadata, B response status, `X-Request-ID`, and stable error codes. Preserve the current user-facing messages and do not log the full authorization URL, PKCE values, DPoP proof, JWK, or tokens.

- [ ] **Step 4: Run focused tests and the full client suite**

  Run: `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts src/adapters/tauriAuthorization.test.ts src/adapters/tauriHttpTransport.test.ts`

  Run: `npm run test:run --workspace @termflow/tauri-client`

  Expected: all tests pass.

- [ ] **Step 5: Commit authorization diagnostics**

  ```bash
  git add apps/clients/tauri/src/nativeAuth.ts apps/clients/tauri/src/adapters/tauriAuthorization.ts apps/clients/tauri/src/adapters/tauriHttpTransport.ts apps/clients/tauri/src/views/NativeConnectView.vue apps/clients/tauri/src/views/NativeConnectView.test.ts apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts
  git commit -m "feat(tauri): trace native authorization stages"
  ```

### Task 4: Document A/Tauri paths and B/Web C correlation

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/troubleshooting.md`
- Modify: `apps/clients/README.md`
- Create: `apps/clients/tauri/src-tauri/tests/logging_capability_scope.rs` only if the generated capability contract needs a dedicated assertion

- [ ] **Step 1: Add documentation tests or static assertions for documented paths**

  Assert that the A log path uses `platformdirs.user_log_path`, Tauri uses `app_log_dir`, and no Docker file-log environment variable is introduced.

- [ ] **Step 2: Update operator documentation**

  Document platform paths, log rotation, secret redaction, `docker compose logs -f control-plane`, and the expected native OAuth event sequence. Explain that Web C diagnostic correlation uses the `X-Request-ID` response header.

- [ ] **Step 3: Run documentation and privacy checks**

  Run: `git diff --check`

  Run: `rg -n "TERMFLOW_LOG_DIR|control-plane\.log" deploy .env.example README.md docs || true`

  Expected: no Docker file-log configuration; documentation points to stdout/stderr for B.

- [ ] **Step 4: Commit documentation**

  ```bash
  git add README.md docs/operations.md docs/troubleshooting.md apps/clients/README.md
  git commit -m "docs: document TermFlow log locations"
  ```

### Task 5: Cross-platform verification and release readiness

**Files:**
- Modify only if verification exposes a concrete issue in the files above.

- [ ] **Step 1: Run A checks**

  ```bash
  uv run --all-packages ruff check apps/node/src apps/node/tests
  uv run --package termflow-node mypy apps/node/src/termflow_node
  uv run --package termflow-node pytest apps/node/tests -q
  ```

- [ ] **Step 2: Run client checks**

  ```bash
  npm run test:run --workspace @termflow/tauri-client
  npm run typecheck --workspace @termflow/tauri-client
  cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --lib
  ```

- [ ] **Step 3: Run repository verification**

  ```bash
  npm run test:run
  npm run typecheck
  uv run --all-packages pytest -q
  git diff --check
  ```

- [ ] **Step 4: Verify the Windows diagnostic path**

  Build the Windows package through the existing tagged/manual workflow, install it, trigger the registration failure, and confirm a JSONL log appears under `%LOCALAPPDATA%\\io.termflow.client\\logs\\termflow-client.log` without credentials or query parameters.

- [ ] **Step 5: Report evidence and remaining platform limits**

  Separate local Linux verification, Rust compilation, CI package evidence, and live Windows installation evidence. Do not claim Windows runtime verification until the rebuilt installer has been run.
