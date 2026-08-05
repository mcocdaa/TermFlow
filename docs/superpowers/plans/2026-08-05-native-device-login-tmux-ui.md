# Native Device Login, Tmux Isolation, and Authorization UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make computer/Term deletion confirmations consistent, make frozen A builds use host tmux libraries, and deliver a clear same-device and cross-device authorization flow on Tauri and Web C.

**Architecture:** Shared `packages/client-ui` owns Web C approval and reusable UI; Tauri owns native connection screens, device-code polling, secure-key exchange, and native diagnostics. The A fix stays at the subprocess boundary so `termflow doctor` and all tmux calls use host libraries instead of PyInstaller private libraries.

**Tech Stack:** Vue 3, Vue Router, Vitest/jsdom, Lucide Vue, Tauri 2 HTTP/deep-link plugins, Rust/reqwest, Python/PyInstaller, existing TermFlow API contracts.

---

## Starting state and file map

The working tree already has partial changes that must be preserved:

- `apps/node/src/termflow_node/tmux/runner.py` removes both frozen-library variables, and its regression test passes.
- `packages/client-ui/src/components/computers/DeleteComputerDialog.vue` plus the ComputerTable/ComputersView wiring and tests are present and pass their focused suite.
- `apps/clients/tauri/src/views/NativeConnectView.test.ts` expects the new action names, but `NativeConnectView.vue` is not updated yet; Task 4 completes that red/green cycle.

| Responsibility | Files |
| --- | --- |
| A tmux/doctor | `apps/node/src/termflow_node/tmux/runner.py`, `apps/node/tests/test_tmux_runner.py`, `apps/node/tests/test_diagnostics.py` |
| Computer deletion | `packages/client-ui/src/components/computers/DeleteComputerDialog.vue`, `ComputerTable.vue`, `ComputersView.vue`, matching tests |
| Web C entry/approval | `packages/client-ui/src/App.vue`, `DashboardView.vue`, `LoginView.vue`, `DeviceAuthorizeView.vue`, matching tests |
| Tauri authorization | `apps/clients/tauri/src/views/NativeConnectView.vue`, `NativeDeviceAuthorizeView.vue`, matching tests |
| Native diagnostics/capability | `apps/clients/tauri/src/adapters/tauriHttpTransport.ts`, `nativeAuth.ts`, `adapters/tauriAuthorization.ts`, `src-tauri/capabilities/*.json`, capability tests |
| Shared styles | `packages/client-ui/src/styles/app.css` |

## Task 1: Finish and verify frozen A tmux isolation

**Files:** `apps/node/src/termflow_node/tmux/runner.py`, `apps/node/tests/test_tmux_runner.py`, `apps/node/tests/test_diagnostics.py`

- [ ] **Step 1: Prove the regression test fails against the old behavior.** Use both variables pointing to `/tmp/pyinstaller-private` and assert the captured subprocess environment contains neither variable:

```python
monkeypatch.setattr(runner_module.sys, "frozen", True, raising=False)
monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pyinstaller-private")
monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/pyinstaller-private")
runner_module._subprocess_run(["tmux", "-V"], capture_output=True, text=True, check=False)
assert "LD_LIBRARY_PATH" not in captured["env"]
assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]
```

Run `./.venv/bin/pytest apps/node/tests/test_tmux_runner.py::test_frozen_tmux_process_does_not_expose_pyinstaller_library_paths -q`; the pre-fix result is a failing assertion because the old implementation restores `LD_LIBRARY_PATH_ORIG`.

- [ ] **Step 2: Keep the minimal implementation.** In the frozen branch copy `os.environ`, run `environment.pop("LD_LIBRARY_PATH", None)` and `environment.pop("LD_LIBRARY_PATH_ORIG", None)`, then return it. Do not use `LD_PRELOAD`, hard-code `/usr/bin/tmux`, or change non-frozen behavior.

- [ ] **Step 3: Add a doctor regression.** Patch `diagnostics.TmuxRunner` with a fake that records construction and returns healthy `is_alive`; assert `run_diagnostics(..., repair=False)` reports the `tmux` check as healthy. This keeps `doctor` on the same sanitized runner path.

- [ ] **Step 4: Verify source and fresh bundle.** Run:

```bash
./.venv/bin/pytest apps/node/tests/test_tmux_runner.py apps/node/tests/test_diagnostics.py -q
bash scripts/release/build_node_bundle.sh 0.0.1-dev.0 /tmp/termflow-node-bundle-check
/tmp/termflow-node-bundle-check/termflow-node-linux-x86_64/termflow doctor
```

The fresh bundle must not report `libtinfo.so.6` or `NCURSES6_TINFO_6.4.current`; a missing host tmux must report “tmux is not installed”.

- [ ] **Step 5: Commit:** `git add apps/node/src/termflow_node/tmux/runner.py apps/node/tests/test_tmux_runner.py apps/node/tests/test_diagnostics.py && git commit -m "fix(node): isolate system tmux from frozen libraries"`.

## Task 2: Complete the shared computer deletion dialog

**Files:** `packages/client-ui/src/components/computers/DeleteComputerDialog.vue`, `ComputerTable.vue`, `ComputersView.vue`, `DeleteComputerDialog.test.ts`, `ComputersView.test.ts`

- [ ] **Step 1: Add component tests first.** Mount an offline computer, focus the invoker, assert `role="alertdialog"`, `aria-modal="true"`, initial focus on `[data-action="cancel-delete-computer"]`, confirm emission with the installation ID, Escape/backdrop cancellation, and focus restoration. With `pending: true`, both buttons are disabled and neither Escape nor backdrop cancels.

- [ ] **Step 2: Run the new test before the component exists:** `npm run test:run --workspace @termflow/client-ui -- src/components/computers/DeleteComputerDialog.test.ts`; expect import/selector failure.

- [ ] **Step 3: Keep `ComputerTable.vue` presentational.** Offline delete emits `remove`; it never calls `window.confirm`. Computers with online Terms remain disabled with their existing accessible reason.

- [ ] **Step 4: Keep view-owned state.** `ComputersView.vue` owns `selectedForDeletion`, `deletingId`, and `deleteError`. Confirm calls `runtime.api.computers.remove`; success removes the row, closes the dialog, and shows the existing “已删除” bottom toast; failure keeps the dialog open with an inline alert; cancel does not call the API.

- [ ] **Step 5: Run:** `npm run test:run --workspace @termflow/client-ui -- src/components/computers/DeleteComputerDialog.test.ts src/views/ComputersView.test.ts`; expect all tests pass.

- [ ] **Step 6: Commit:** `git add packages/client-ui/src/components/computers packages/client-ui/src/views/ComputersView.vue packages/client-ui/src/views/ComputersView.test.ts && git commit -m "feat(web): confirm computer deletion in a modal"`.

## Task 3: Make Web C device authorization discoverable

**Files:** `packages/client-ui/src/views/LoginView.vue`, `DashboardView.vue`, `DeviceAuthorizeView.vue`, `LoginView.test.ts`, `DeviceAuthorizeView.test.ts`, `App.test.ts`

- [ ] **Step 1: Add failing entry assertions.** Login must expose a RouterLink to `/device` (for example “已有设备码？授权设备”); the Control Center heading action area must expose one “设备授权” RouterLink to `/device`.

- [ ] **Step 2: Run red tests:** `npm run test:run --workspace @termflow/client-ui -- src/views/LoginView.test.ts src/views/DeviceAuthorizeView.test.ts src/App.test.ts`; the new link assertions must fail before implementation.

- [ ] **Step 3: Implement both entry points without adding a fourth fixed navigation item.** `/device` remains `requiresAuth`; unauthenticated users go to `/login?redirect=...` and the full code query is preserved.

- [ ] **Step 4: Improve `DeviceAuthorizeView.vue`.** Add one “返回控制中心” link, separate the code field from its action with existing form spacing, and center the preview card. Keep only client name, platform/version, scopes, expiry, optional TOTP, and “拒绝/允许此设备”. Do not change `deviceAuthorizationPreview` or `decideAuthorization` contracts.

- [ ] **Step 5: Verify and commit:**

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/LoginView.test.ts src/views/DeviceAuthorizeView.test.ts src/App.test.ts
git add packages/client-ui/src/views packages/client-ui/src/App.test.ts
git commit -m "feat(web): expose device authorization entry"
```

## Task 4: Redesign the Tauri connection page

**Files:** `apps/clients/tauri/src/views/NativeConnectView.vue`, `NativeConnectView.test.ts`, `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Extend the existing failing test.** Assert two `.native-auth-option` elements, `[data-action="browser-login"]` text containing “本机浏览器登录”, and `[data-action="device-authorize"]` text containing “其他设备授权”. Keep metadata/redirect assertions and update the pending label to “等待浏览器审批…”.

- [ ] **Step 2: Run red:** `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts`; expect missing selector failures.

- [ ] **Step 3: Implement two equal-width actions.** Preserve the server URL form and `authorizeNativeClient` for the same-device path. The submit button is “本机浏览器登录”; the second button routes to `/connect/device` and is “其他设备授权”. Remove B/Web C/native-client explanations from the default page copy.

- [ ] **Step 4: Implement hover/focus help on the buttons themselves.** Do not add a `?` icon. Each button wrapper owns a sibling tooltip hidden by default and shown by `:hover`/`:focus-within`, with `aria-describedby`; the tooltip explains system-browser login or Web C device-code approval and wraps within the card.

- [ ] **Step 5: Run `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts` and `npm run typecheck --workspace @termflow/tauri-client`; then commit with `git commit -m "feat(tauri): clarify native authorization choices"`.

## Task 5: Apply the approved two-column Tauri device-code layout

**Files:** `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`, `NativeDeviceAuthorizeView.test.ts`, `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Add failing structure assertions.** After starting, assert `.native-device-layout`, a `[data-action="copy-device-code"]` icon button beside the code, no bottom “复制设备码” button, one bottom “返回”, one “重新生成”, and no “取消” or second “返回连接”.

- [ ] **Step 2: Run red:** `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeDeviceAuthorizeView.test.ts`.

- [ ] **Step 3: Implement the final state.** Before generation show URL and “生成设备授权码”. After generation, left column contains themed QR and “扫码打开授权页”; right column contains code, adjacent copy SVG, visible status/remaining time, and one concise instruction. The bottom actions are only “返回” (cancel + route to `/connect`) and “重新生成” (cancel old session + start again). This flow never calls `openUrl`.

- [ ] **Step 4: Add responsive CSS.** Use two columns above `48rem`, one column below; keep QR theme color, focus-visible styles, and code wrapping without pushing actions off-screen.

- [ ] **Step 5: Run tests/typecheck and commit:** `npm run test:run --workspace @termflow/tauri-client -- src/views/NativeDeviceAuthorizeView.test.ts && npm run typecheck --workspace @termflow/tauri-client`, then `git commit -m "feat(tauri): redesign device authorization screen"`.

## Task 6: Diagnose Windows authorization failures without leaking secrets

**Files:** `apps/clients/tauri/src/adapters/tauriHttpTransport.ts`, `nativeAuth.ts`, `adapters/tauriAuthorization.ts`, `apps/clients/tauri/src-tauri/capabilities/default.json`, `mobile.json`, `src-tauri/tests/http_capability_scope.rs`, adapter tests

- [ ] **Step 1: Add failing log assertions.** Mock `logNativeEvent` and assert HTTP, browser-open, callback, and token failures log bounded sanitized details containing no access token, refresh token, DPoP proof, request body, or full query string.

- [ ] **Step 2: Implement the sanitizer at each boundary.** Replace URLs with `<url>`, callbacks with `<callback>`, cap detail at 256 characters, and log only issuer origin/path, request ID, event, level, and error code/detail. Keep the user-facing error generic.

- [ ] **Step 3: Keep loopback capability exact.** The plugin-valid IPv6 pattern is the JSON string `http://[\\:\\:1]:*`; test the final capability against HTTPS, `127.0.0.1`, `localhost`, and `[::1]` allowed cases and remote HTTP denied. Do not allow arbitrary remote HTTP.

- [ ] **Step 4: Run:**

```bash
npm run test:run --workspace @termflow/tauri-client -- src/adapters/tauriHttpTransport.test.ts src/adapters/tauriAuthorization.test.ts
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --test http_capability_scope
```

If local WSL lacks `pkg-config`/DBus headers, use the CI Tauri builder for the Rust test and report the local environment limitation.

- [ ] **Step 5: Commit:** `git add apps/clients/tauri/src apps/clients/tauri/src-tauri/capabilities apps/clients/tauri/src-tauri/tests && git commit -m "fix(tauri): diagnose native authorization failures"`.

## Task 7: Isolated visual, trajectory, and artifact verification

**Files:** `apps/clients/web/e2e/control-center.spec.ts` and `settings-auth.spec.ts` only when stable selectors are needed; no production data or existing container may be used.

- [ ] **Step 1: Run focused suites and typechecks:**

```bash
./.venv/bin/pytest apps/node/tests/test_tmux_runner.py apps/node/tests/test_diagnostics.py -q
npm run test:run --workspace @termflow/client-ui -- src/components/computers/DeleteComputerDialog.test.ts src/views/ComputersView.test.ts src/views/LoginView.test.ts src/views/DeviceAuthorizeView.test.ts src/App.test.ts
npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts src/views/NativeDeviceAuthorizeView.test.ts src/adapters/tauriHttpTransport.test.ts src/adapters/tauriAuthorization.test.ts
npm run typecheck --workspace @termflow/client-ui
npm run typecheck --workspace @termflow/tauri-client
```

- [ ] **Step 2: Use the isolated browser workflow** with a fresh image tag, unique loopback port, disposable data, and screenshots for login, device-code input, two-column device state, Web C approval, and computer deletion. Confirm the existing service/container/volume remain unchanged.

- [ ] **Step 3: Verify both authorization trajectories:** same-device system-browser callback and cross-device device-code approval. The latter must complete without opening a browser from Tauri.

- [ ] **Step 4: Install a fresh Windows artifact and inspect the normalized log path** (`termflow-client.log` with `.1`–`.5` rotations). A failure must identify HTTP capability, browser open, callback delivery, or token exchange; it must not expose credentials.

- [ ] **Step 5: Run `./scripts/verify.sh` and report platform evidence separately.** Local WSL tests do not prove a Windows installer or iOS/Android build; those require completed CI jobs and a fresh artifact.

## Plan self-review

- Coverage includes both deletion modals, the bundled `libtinfo`/tmux root cause, Tauri two-path login, Web C device-code discovery, approved QR/status layout, hover-only help, Windows diagnostics, loopback capabilities, and isolated visual verification.
- Every task names exact files, tests, commands, selectors, and expected outcomes; no unfinished placeholder markers remain.
- The plan does not change OAuth contracts, token storage, Docker logging, or permit insecure remote HTTP.
