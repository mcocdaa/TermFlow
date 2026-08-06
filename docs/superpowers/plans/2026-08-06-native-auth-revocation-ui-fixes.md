# Native Auth, Revocation, and UI Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make revoked A installations recover cleanly, prevent frozen A bundles from leaking private libraries to tmux, and make the Web C/Tauri authorization flow visibly consistent and verifiably complete on Windows.

**Architecture:** Keep B deletion semantics unchanged: deleting a computer revokes B-side installation/instance records, while A learns that state on its next authenticated sync. A will only replace an existing local login automatically after the same server proves that the old installation is unauthorized; active or unreachable installations still require the explicit `--force` safety switch. Tauri will verify the newly stored native credential before navigating away from the connection screen, so a failed protected request is shown on the connection page instead of silently redirecting back to it. Web C and Tauri will reuse the existing dialog, focus, toast, QR, and theme primitives rather than introducing another modal system.

**Tech Stack:** Python 3.12, Typer, httpx, pytest; Vue 3, TypeScript, Vitest, Vue Test Utils; Tauri 2, Rust keyring/DPoP; Playwright isolated browser runs; GitHub Actions package workflows.

---

## Scope and file map

The work has three independently testable tracks, executed serially so shared authentication behavior is settled before visual verification:

- A lifecycle/runtime: `apps/node/src/termflow_node/cli.py`, `apps/node/src/termflow_node/control_plane_client.py`, `apps/node/tests/test_login.py`, `apps/node/tests/test_cli_lifecycle.py`, `apps/node/tests/test_tmux_runner.py`.
- Web C authorization settings: `packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue`, `packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.vue`, `packages/client-ui/src/styles/app.css`, and their Vitest tests plus `apps/clients/web/e2e/settings-auth.spec.ts`.
- Tauri native completion/layout: `apps/clients/tauri/src/nativeAuth.ts`, `apps/clients/tauri/src/views/NativeConnectView.vue`, `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`, their tests, and the existing capability contract in `apps/clients/tauri/src-tauri/capabilities/default.json`/`mobile.json`.

No B API deletion contract or database migration is required for these six reports. The installed Windows executable and the downloaded Linux bundle are release artifacts, not source-of-truth code; they must be rebuilt from the fixed commit before runtime claims are made.

### Task 1: Recover an A login only after B confirms revocation

**Files:**
- Modify: `apps/node/src/termflow_node/control_plane_client.py`
- Modify: `apps/node/src/termflow_node/cli.py:55-83`
- Test: `apps/node/tests/test_login.py`

- [ ] **Step 1: Add a status-aware installation probe test**

Add tests using `httpx.MockTransport` that distinguish the three states:

```python
@pytest.mark.asyncio
async def test_installation_probe_reports_revoked_only_for_auth_failures() -> None:
    installation = InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id=uuid4(),
        installation_token="installation-secret-token-that-is-long-enough",
    )

    def revoked(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/instances/mine"
        return httpx.Response(401)

    assert await ControlPlaneClient(transport=httpx.MockTransport(revoked)).installation_revoked(installation)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        await ControlPlaneClient(transport=httpx.MockTransport(unavailable)).installation_revoked(installation)
```

Add a CLI regression test with an existing config and a new enrollment code. Mock `installation_revoked` to return `True` and `enroll` to return a replacement installation; assert the command succeeds without `--force` and the config contains only the replacement installation. Add a second test where the probe returns `False`; assert the command still fails with the `--force` hint.

- [ ] **Step 2: Run the new tests and observe the expected red failure**

Run:

```bash
.venv/bin/python -m pytest apps/node/tests/test_login.py -q
```

Expected before implementation: `AttributeError` for the missing probe and the revoked-login test fails because `login` still rejects every existing config before contacting B.

- [ ] **Step 3: Implement a conservative probe**

Add this method to `ControlPlaneClient`:

```python
async def installation_revoked(self, installation: InstallationConfig) -> bool:
    base_url = validate_server_url(str(installation.server_url))
    async with httpx.AsyncClient(transport=self._transport, timeout=3.0) as client:
        response = await client.get(
            f"{base_url}/api/v1/instances/mine",
            headers={"Authorization": "Bearer " + installation.installation_token.get_secret_value()},
        )
    if response.status_code in {401, 403, 404}:
        return True
    response.raise_for_status()
    return False
```

In `cli.login`, normalize the requested server first. When a local config exists and `--force` is absent, probe only if the stored server equals the requested server. Continue enrollment only when the probe returns `True`; retain the existing safety error for an active installation, a different server, or a probe/network error. Do not print or log either token.

- [ ] **Step 4: Run the focused tests and the existing A lifecycle tests**

Run:

```bash
.venv/bin/python -m pytest apps/node/tests/test_login.py apps/node/tests/test_cli_lifecycle.py -q
```

Expected: all focused tests pass, including the original “existing login requires `--force`” assertion for active installations.

- [ ] **Step 5: Commit the A login behavior**

```bash
git add apps/node/src/termflow_node/control_plane_client.py apps/node/src/termflow_node/cli.py apps/node/tests/test_login.py
git commit -m "fix(node): recover login after installation revocation"
```

### Task 2: Stop the frozen A launcher from poisoning the user-facing tmux attach

**Files:**
- Modify: `apps/node/src/termflow_node/cli.py:17,134-143`
- Test: `apps/node/tests/test_cli_lifecycle.py`

- [ ] **Step 1: Add a failing launcher-environment test**

Add a small test around a new private helper, so the test observes the exact environment passed to the final `tmux attach-session` process:

```python
def test_exec_tmux_removes_frozen_library_paths(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_exec(file, argv, env):
        captured.update(file=file, argv=argv, env=env)
        raise SystemExit

    monkeypatch.setattr(cli.os, "execvpe", fake_exec)
    from termflow_node.tmux import runner as runner_module

    monkeypatch.setattr(runner_module.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pyinstaller-private")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/pyinstaller-private")

    with pytest.raises(SystemExit):
        cli._exec_tmux(["tmux", "-S", "/tmp/termflow.sock", "attach-session"])

    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]
```

Use the existing `tmux_subprocess_environment` tests as the reference; import the module rather than duplicating environment policy in the CLI test.

- [ ] **Step 2: Run the regression test and confirm it fails on `execvp`**

Run:

```bash
.venv/bin/python -m pytest apps/node/tests/test_cli_lifecycle.py::test_exec_tmux_removes_frozen_library_paths -q
```

Expected: FAIL because `_exec_tmux` does not exist and `new`/`attach` currently call `os.execvp`, which inherits the PyInstaller `LD_LIBRARY_PATH`.

- [ ] **Step 3: Delegate the final tmux exec through the existing policy**

Import `NoReturn` from `typing`, import `tmux_subprocess_environment` from `termflow_node.tmux.runner`, and add:

```python
def _exec_tmux(argv: list[str]) -> NoReturn:
    os.execvpe(argv[0], argv, tmux_subprocess_environment())
```

Replace both `os.execvp(argv[0], argv)` calls in `new` and `attach` with `_exec_tmux(argv)`. The private tmux server, control client, and PTY paths already use the same environment policy and must not be changed.

- [ ] **Step 4: Run unit and installed-bundle verification**

Run:

```bash
.venv/bin/python -m pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_tmux_runner.py apps/node/tests/test_remote_client.py -q
scripts/release/verify_node_bundle.sh v0.0.1-dev.0
```

Expected: the launcher test passes, the bundle verification completes with the installed `termflow new`/tmux attach path, and no `libtinfo.so.6` version warning appears. The temporary verification directory is cleaned by the script.

- [ ] **Step 5: Commit the runtime fix**

```bash
git add apps/node/src/termflow_node/cli.py apps/node/tests/test_cli_lifecycle.py
git commit -m "fix(node): sanitize frozen environment before tmux attach"
```

### Task 3: Unify Web C approval and revocation dialogs

**Files:**
- Modify: `packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue`
- Modify: `packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Create: `packages/client-ui/src/components/settings/AuthorizedClientsPanel.test.ts`
- Test: `packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.test.ts`
- Modify: `apps/clients/web/e2e/settings-auth.spec.ts`

- [ ] **Step 1: Add red tests for modal semantics and focus behavior**

Create a focused `AuthorizedClientsPanel` test with one active client. Assert that clicking “撤销” renders a `role="alertdialog"` with the same `.dialog-backdrop`/`.dialog-panel` structure used by Delete Term/Delete Computer, includes the selected client name, and does not render the administrator token input before the modal is opened. Assert Escape or the cancel action closes it, and submitting calls `runtime.api.clients.remove` with the entered credentials.

Extend the approval dialog test to assert a stable header/body/action layout:

```ts
expect(wrapper.get('[data-action="device-approval-dialog"]')).toHaveClass('device-approval-dialog')
expect(wrapper.get('.device-approval-details')).toBeVisible()
expect(wrapper.get('.dialog-actions')).toBeVisible()
```

- [ ] **Step 2: Run the tests and verify the expected red state**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- components/settings/AuthorizedClientsPanel.test.ts components/settings/DeviceAuthorizationApprovalDialog.test.ts
```

Expected: the new revoke test fails because the panel currently renders an inline `<form>` rather than a dialog, and the new class/spacing assertions fail before the CSS/template change.

- [ ] **Step 3: Replace the inline revoke form with a focus-safe alert dialog**

Move the selected-client form into:

```vue
<div v-if="selected" class="dialog-backdrop" @click.self="cancel">
  <section ref="revokePanel" class="dialog-panel revoke-client-dialog" role="alertdialog" aria-modal="true" aria-labelledby="revoke-client-title" @keydown="trapFocus">
    <header><div><p class="eyebrow">Access</p><h2 id="revoke-client-title">撤销客户端</h2></div><button class="icon-button icon-only" type="button" aria-label="关闭" @click="cancel">×</button></header>
    <p>将注销 <strong>{{ selected.display_name }}</strong>，不会影响其他客户端或电脑端 A。</p>
    <form class="security-form" @submit.prevent="removeSelected">
      <label for="revoke-admin-token">管理员 Token</label>
      <input id="revoke-admin-token" ref="revokeInput" v-model="adminToken" type="password" autocomplete="off" required />
      <label v-if="totpEnabled" for="revoke-totp">当前验证码</label>
      <input v-if="totpEnabled" id="revoke-totp" v-model="totpCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required />
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <div class="dialog-actions"><button type="button" class="secondary-button" @click="cancel">取消</button><button type="submit" class="danger-button">确认撤销</button></div>
    </form>
  </section>
</div>
```

Use the same `nextTick` focus capture/restore and Tab/Escape handling as `DeleteComputerDialog.vue`; clear admin/TOTP fields on close and after a successful removal. Add an `error` ref, catch the remove request with the established safe message, and keep the dialog open on failure. On success call `toast.show({ text: '已撤销', tone: 'success' })`, close the dialog, and refresh the active-client list.

- [ ] **Step 4: Apply the shared visual rhythm**

Add only focused styles to `app.css`:

```css
.device-approval-dialog,
.revoke-client-dialog { width: min(100%, 36rem); display: grid; gap: var(--space-4); }
.device-approval-dialog .security-form,
.revoke-client-dialog .security-form { width: 100%; margin-block-start: 0; }
.device-approval-details { margin: 0; }
.device-approval-details div { align-items: baseline; }
.revoke-client-dialog > p { margin: 0; color: var(--color-text-secondary); line-height: 1.6; }
```

Ensure the existing mobile rule allows both dialogs to use the full available width and stacks action buttons only when the viewport is too narrow for readable labels.

- [ ] **Step 5: Run component tests and an isolated desktop/mobile browser review**

Run:

```bash
npm run test:run --workspace @termflow/client-ui
TERMFLOW_E2E_KEEP=1 scripts/run-web-e2e.sh apps/clients/web/e2e/settings-auth.spec.ts --project=desktop --project=mobile
```

Add screenshots before approval, after approval, and while the revoke dialog is open. Inspect the actual screenshots at both viewport sizes: dialog centered, header/body/actions separated, no horizontal overflow, and the established bottom Toast visible only after success. Record the evidence directory in the implementation commit message.

- [ ] **Step 6: Commit the Web C dialog/UI change**

```bash
git add packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue packages/client-ui/src/components/settings/AuthorizedClientsPanel.test.ts packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.vue packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.test.ts packages/client-ui/src/styles/app.css apps/clients/web/e2e/settings-auth.spec.ts
git commit -m "fix(web): unify native client authorization dialogs"
```

### Task 4: Make Tauri completion observable and widen the device authorization page

**Files:**
- Modify: `apps/clients/tauri/src/nativeAuth.ts`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue`
- Modify: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`
- Test: `apps/clients/tauri/src/views/NativeConnectView.test.ts`
- Test: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`
- Create: `apps/clients/tauri/src/nativeAuth.test.ts`
- Verify only: `apps/clients/tauri/src-tauri/capabilities/default.json`, `mobile.json`, `apps/clients/tauri/src-tauri/tests/http_capability_scope.rs`

- [ ] **Step 1: Add a failing completion test**

Add a view test where `session.authorize()` resolves but the first protected `runtime.api.sessions.status()` rejects with `new ApiError('http_capability_denied')`. Assert the device/connect view remains mounted and shows the actionable capability message rather than navigating to `/`. Add a success case where `sessions.status()` resolves and assert the router reaches the requested route and the shared bottom Toast is `{ text: '已连接', tone: 'success' }`.

- [ ] **Step 2: Run the Tauri tests and confirm the missing verification is red**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts src/views/NativeDeviceAuthorizeView.test.ts
```

Expected: the new failure-path test fails because both views currently navigate immediately after the native exchange and leave route-guard failures outside the view’s `try/catch`.

- [ ] **Step 3: Verify the native credential before leaving the connection route**

Import `ClientRuntime` as a type and add this shared helper in `nativeAuth.ts`:

```ts
export async function verifyNativeConnection(runtime: Pick<ClientRuntime, 'api'>): Promise<void> {
  await runtime.api.sessions.status()
}
```

Call it after `authorizeNativeClient` resolves in `NativeConnectView.connect` and after `result.session.authorize()` resolves in `NativeDeviceAuthorizeView.start`, before showing the success Toast or replacing the route. Reuse the existing error-to-message mapping so capability, offline, and authorization failures remain actionable. Do not put administrator tokens or access tokens in the UI/logs.

- [ ] **Step 4: Widen and center the Tauri device-code layout**

Add scoped styles to `NativeDeviceAuthorizeView.vue`:

```css
.device-auth-card { width: min(100%, 56rem); }
.native-device-layout { width: min(100%, 48rem); margin-inline: auto; grid-template-columns: minmax(15rem, 18rem) minmax(0, 1fr); }
@media (max-width: 42rem) {
  .device-auth-card { width: 100%; }
  .native-device-layout { width: 100%; grid-template-columns: 1fr; }
}
```

Keep the existing QR/code grouping and adjacent SVG copy button. The desktop composition must place QR/code and server/status information in balanced columns; mobile must stack them without clipped text or a horizontal scrollbar. “返回” and “重新生成” stay together at the bottom; no duplicate “取消/返回连接” actions are added.

- [ ] **Step 5: Run all Tauri checks and inspect a browser-rendered Tauri build**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client
npm run typecheck --workspace @termflow/tauri-client
npm run tauri:build --workspace @termflow/tauri-client -- --no-bundle
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
```

Use the existing component test harness plus the freshly built native app/WebView at desktop and mobile viewport sizes to capture the connection page and device-code page. Verify the card width, no clipped headings, the QR/code column balance, and that an authorization failure stays visible on the connection page. Confirm the Rust capability contract still allows HTTPS plus `127.0.0.1`, `localhost`, and `[::1]`, and that both capability files use `opener:allow-default-urls`.

- [ ] **Step 6: Rebuild and install the Windows artifact before claiming Windows is fixed**

Use the current commit’s tag/version context and the existing reusable workflow:

```bash
gh workflow run tauri-packages.yml -f platform=windows -f version=0.0.1-dev.0
gh run watch --exit-status
```

If GitHub CLI authentication is unavailable, use the GitHub Actions UI to run the same `platform=windows` manual workflow and download its artifact. Install that fresh executable, then exercise both “本机浏览器登录” and “其他设备授权” against an isolated B. Inspect the Windows log at `%LOCALAPPDATA%\\io.termflow.client\\logs\\termflow-client.log` and require:

- no `opener:allow-open-url` ACL error;
- no `http_capability_denied` for an allowed HTTPS or loopback URL;
- a successful protected request after token exchange;
- navigation from the connection page to the requested workspace.

- [ ] **Step 7: Commit the Tauri completion/layout change**

```bash
git add apps/clients/tauri/src/nativeAuth.ts apps/clients/tauri/src/views/NativeConnectView.vue apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue apps/clients/tauri/src/views/NativeConnectView.test.ts apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts apps/clients/tauri/src/nativeAuth.test.ts
git commit -m "fix(tauri): verify native auth completion and widen device flow"
```

### Final verification and handoff

- [ ] Run the full repository verification from a clean checkout/worktree:

```bash
scripts/verify.sh
```

- [ ] Run the isolated browser suite again on desktop and mobile and preserve its screenshot directory.

- [ ] Run `git status --short --branch`, verify the three focused commits plus this plan are present, and confirm no Docker container, named volume, or user installation was modified by the isolated tests.

- [ ] Report separately: source tests, isolated browser screenshots, freshly rebuilt Windows artifact, and anything still requiring a real user-owned server/certificate or device.
