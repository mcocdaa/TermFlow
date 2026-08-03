# TermFlow Client Release and UI Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore native authorization on supported server URLs, make native approval and shared settings UI match the product wording/layout, and publish installable unsigned test artifacts when a version tag is pushed.

**Architecture:** Keep the existing B OAuth transaction and system-browser consent flow unchanged. Tauri remains the native composition root and receives narrowly scoped HTTPS plus loopback HTTP permissions; shared Vue owns the fixed application shell, themed QR rendering, and TOTP presentation. CI continues to prove compilation on every push, while a separate `v*` tag workflow builds and collects platform-native unsigned test packages.

**Tech Stack:** Tauri 2, Rust, Vue 3, TypeScript/Vitest, Playwright, Python/pytest contract tests, GitHub Actions.

---

### Task 1: Repair mobile keyring initialization and native HTTP authorization

**Files:**
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`
- Modify: `apps/clients/tauri/src-tauri/src/lib.rs`
- Modify: `apps/clients/tauri/src-tauri/capabilities/default.json`
- Modify: `apps/clients/tauri/src-tauri/capabilities/mobile.json`
- Modify: `tests/tauri/test_mobile_keyring_contract.py`
- Create: `tests/tauri/test_native_network_capability_contract.py`

- [ ] Add failing contract assertions that mobile store initialization passes a closure to `Result::map`, both capabilities allow `https://*` plus loopback HTTP patterns, and neither capability allows arbitrary public HTTP.
- [ ] Run `python -m pytest tests/tauri/test_mobile_keyring_contract.py tests/tauri/test_native_network_capability_contract.py -q`; expect the new assertions to fail.
- [ ] Replace the function item with `map(|store| keyring_core::set_default_store(store))`, use cfg shadowing instead of a mobile-only unnecessary mutable builder, and add the same HTTP allow scope to desktop/mobile capabilities.
- [ ] Re-run the two tests and `cargo fmt --manifest-path apps/clients/tauri/src-tauri/Cargo.toml -- --check`; expect success.

### Task 2: Make native connection request state explicit and errors actionable

**Files:**
- Create: `apps/clients/tauri/src/views/NativeConnectView.test.ts`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue`

- [ ] Write a failing component test that requires `Connect to Server / 连接到服务器`, removes all B/Web-C implementation copy, labels the input `服务器地址`, starts with `申请注册远程控制`, and changes to disabled `等待服务器管理员审批` while the authorization promise is pending.
- [ ] Add error mapping tests that distinguish unreachable server, invalid address, metadata mismatch, browser/deep-link failure, and token exchange failure without revealing credentials.
- [ ] Run the single Vitest file and observe requirement failures.
- [ ] Implement the minimal presentation/state/error mapping while preserving `authorizeNativeClient` and the system-browser OAuth flow.
- [ ] Re-run the Tauri workspace tests and typecheck.

### Task 3: Fix the shared application shell scrolling contract

**Files:**
- Modify: `packages/client-ui/src/styles/reset.css`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] Add failing CSS and browser assertions that ordinary authenticated routes keep `html/body/#app` and `.app-shell` at `100dvh` with outer overflow hidden, use `minmax(0, 1fr)` for the content row, and make only `main` vertically scrollable.
- [ ] Run the responsive contract test and confirm the new assertions fail.
- [ ] Implement the fixed desktop/mobile grid shell without altering terminal route behavior or bare login/authorization pages.
- [ ] Re-run responsive/unit tests and verify dashboard, computers, and settings each scroll inside `main` while header/nav positions remain unchanged.

### Task 4: Refine TOTP help, onboarding layout, spacing, and QR colors

**Files:**
- Modify: `packages/client-ui/src/components/settings/TotpPanel.vue`
- Modify: `packages/client-ui/src/views/TotpActivationView.vue`
- Modify: `packages/client-ui/src/components/common/QrCodeDialog.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/design-tokens/src/themes/graphite-signal.css`
- Modify: `packages/design-tokens/src/themes/cloud-cobalt.css`
- Modify: `packages/design-tokens/src/themes/midnight-indigo.css`
- Modify: `packages/design-tokens/src/contract.test.ts`
- Modify: relevant Vue component tests and `apps/clients/web/e2e/settings-auth.spec.ts`

- [ ] Add failing tests for a one-line `启用双重认证登录` label followed by an accessible question-mark SVG tooltip, equal button heights, centered onboarding content, QR dialog spacing, and non-black/non-white theme QR tokens.
- [ ] Run targeted UI/token tests and confirm the new assertions fail for the intended reasons.
- [ ] Reuse the existing help-tooltip pattern and `ThemedQrCode`; adjust layout classes and set each QR foreground/background to its accent and themed page/panel colors with sufficient contrast.
- [ ] Re-run component tests, theme contracts, typecheck, and the isolated settings browser trajectory.

### Task 5: Add tag-triggered multi-platform packaging

**Files:**
- Create: `.github/workflows/tauri-packages.yml`
- Delete: `.github/workflows/tauri-windows-package.yml`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `docs/operations.md`

- [ ] First change the deployment contract test to require `push.tags: ['v*']`, manual dispatch, Windows NSIS, Linux AppImage/deb, macOS app/dmg, Android debug APK, iOS simulator zip, and uploaded workflow artifacts.
- [ ] Run the contract test and confirm failure against the Windows-only workflow.
- [ ] Implement isolated native jobs using the pinned Node/Rust setup already present in CI. Keep Android as a debug-keystore-signed test APK and iOS as an unsigned simulator `.app` zip; do not claim production/store/device signing or automatically publish unsigned artifacts as a GitHub Release.
- [ ] Update operations documentation with tag/version steps and signing boundaries, then re-run YAML/contract tests.

### Task 6: Integrated verification and review

**Files:** all files above.

- [ ] Run targeted red/green tests for each task, then `npm run test:run`, `npm run typecheck`, and workspace builds.
- [ ] Run relevant Python contracts plus Rust tests/format checks; where Android/iOS SDK compilation is unavailable locally, retain the exact CI limitation and use the target contract checks as local evidence.
- [ ] Run the isolated real-browser settings/layout suite, inspect screenshots in all three themes, and verify no outer document scrolling.
- [ ] Request independent specification and code-quality reviews, fix all findings, and re-run the full gates.
- [ ] Commit the feature branch, merge it to `main`, push, and inspect the resulting CI. A tag release is triggered only after an explicit version tag is pushed; do not create a tag implicitly.
