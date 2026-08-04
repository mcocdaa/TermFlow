# TermFlow Native Dual Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-device OAuth device-code authorization path while preserving the existing same-device browser OAuth + PKCE path for Tauri clients.

**Architecture:** Keep one B authorization transaction model and one native token lifecycle. The browser path continues to use authorization-code + PKCE + `termflow://` callback; the device path creates a short-lived `device_code`/`user_code` pair, lets Web C approve it from any device, and lets Tauri poll the token endpoint. Both paths produce the same DPoP-bound native access/refresh credential and enter the existing `TokenSession`.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, generated TypeScript contracts, `packages/client-core` auth state machines, Vue 3 Web C/Tauri UI, Tauri 2, Vitest, pytest, Rust capability contract tests.

---

## File map and ownership

- `packages/client-contracts/src/generated.ts`: add metadata/device-code/poll response types and stable error unions.
- `packages/client-core/src/api/oauth.ts`: expose device-code creation, polling, Web C preview and decision requests.
- `packages/client-core/src/auth/deviceAuthorization.ts`: pure device authorization polling state machine.
- `packages/client-core/src/auth/deviceAuthorization.test.ts`: pending, slow-down, denial, expiry, cancellation and success tests.
- `apps/control-plane/src/termflow_control_plane/persistence/models.py`: add device-code digest/state fields to the existing `OAuthAuthorization` record.
- `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`: atomic create, lookup, approve/deny and one-time exchange operations.
- `apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0004_oauth_device_flow.py`: migrate device-flow columns/indexes.
- `apps/control-plane/src/termflow_control_plane/auth/oauth.py`: shared device request creation, polling, and exchange logic.
- `apps/control-plane/src/termflow_control_plane/api/oauth.py`: device-code endpoint and device grant branch in `/api/v1/oauth/token`.
- `apps/control-plane/tests/test_oauth_device_flow.py`: backend unit/API/security coverage.
- `packages/client-ui/src/views/DeviceAuthorizeView.vue`: cross-device Web C approval page.
- `packages/client-ui/src/views/DeviceAuthorizeView.test.ts`: UI states, session/TOTP and invalid codes.
- `packages/client-ui/src/router/routes.ts`: register `/device` route.
- `apps/clients/tauri/src/views/NativeConnectView.vue`: show “在本机浏览器授权” and “在其他设备上授权”.
- `apps/clients/tauri/src/nativeAuth.ts`: retain browser authorization and add device authorization entry.
- `apps/clients/tauri/src/adapters/tauriDeviceAuthorization.ts`: Tauri device-code transport and polling wiring.
- `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`: device code/QR/status UI.
- `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`: UI and cancellation coverage.
- `apps/clients/tauri/src-tauri/tests/http_capability_scope.rs`: ensure the device path does not require opener/deep-link permissions.
- `tests/e2e/test_device_authorization.py`: independent Web C approval and native polling.
- `docs/web-client.md`, `docs/operations.md`, `docs/github-actions.md`: explain both authorization paths and expiry behavior.

## Task 1: Freeze contract shapes and metadata

**Files:**
- Modify: `packages/client-contracts/src/generated.ts`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/oauth.py`
- Modify: `apps/control-plane/tests/test_oauth_api.py`
- Test: `packages/client-core/src/api/authApis.test.ts`

- [ ] **Step 1: Write failing contract tests**

Assert metadata in `apps/control-plane/tests/test_oauth_api.py` contains a device authorization endpoint and a verification URI, and define the exact
device response shapes:

```ts
expect(metadata.device_authorization_endpoint).toBe('/api/v1/oauth/device/code')
expect(metadata.device_verification_uri).toBe('/device')
```

The TypeScript response must include `device_code`, `user_code`, `verification_uri`,
`verification_uri_complete`, `expires_in`, and `interval`.

- [ ] **Step 2: Run the focused tests and verify they fail**

```bash
uv run pytest apps/control-plane/tests/test_oauth_api.py -q
npm exec vitest run packages/client-core/src/api/authApis.test.ts
```

Expected failure: metadata and generated types do not contain device-flow fields.

- [ ] **Step 3: Add the contract types and metadata fields**

Use the existing generated contract naming conventions. Add a device-code request type containing the
same client metadata, scopes, DPoP JWK and PKCE challenge used by the current browser request. Add token
poll errors as a discriminated union with `authorization_pending`, `slow_down`, `access_denied`, and
`expired_token`.

- [ ] **Step 4: Run the focused tests and commit**

```bash
uv run pytest apps/control-plane/tests/test_oauth_api.py -q
npm exec vitest run packages/client-core/src/api/authApis.test.ts
git add packages/client-contracts apps/control-plane/src/termflow_control_plane/auth/oauth.py apps/control-plane/tests/test_oauth_api.py packages/client-core/src/api/authApis.test.ts
git commit -m "feat(auth): publish device authorization metadata"
```

## Task 2: Add persistent device-code state with atomic lifecycle

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Create: `apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0004_oauth_device_flow.py`
- Test: `apps/control-plane/tests/test_oauth_device_flow.py`

- [ ] **Step 1: Write failing repository tests**

Cover these exact transitions:

```python
created = await repository.create_device_authorization(request)
assert created.user_code != created.device_code
assert await repository.find_by_device_code(created.device_code) is not None
assert await repository.find_by_user_code(created.user_code) is not None

await repository.mark_approved(created.id)
exchanged = await repository.exchange_device_code(created.device_code, verifier)
assert exchanged is not None
assert await repository.exchange_device_code(created.device_code, verifier) is None
```

Also test expiry, deny, wrong code, concurrent exchange where exactly one caller succeeds, and digest-only
storage (raw device/user code must not appear in persisted columns).

- [ ] **Step 2: Run tests and verify the expected missing-method/schema failures**

```bash
uv run pytest apps/control-plane/tests/test_oauth_device_flow.py -q
```

- [ ] **Step 3: Implement the model, indexes, migration and repository methods**

Store only SHA-256/HMAC-compatible digests for `device_code` and `user_code`; keep expiry, polling interval,
status, authorization ID, and one-time exchange marker. Use a conditional SQL update for approval and
exchange so concurrent requests cannot issue two credentials.

- [ ] **Step 4: Run repository tests and migration checks**

```bash
uv run pytest apps/control-plane/tests/test_oauth_device_flow.py -q
uv run pytest apps/control-plane/tests/test_persistence_migrations.py -q
```

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/persistence/models.py apps/control-plane/src/termflow_control_plane/persistence/repositories.py apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0004_oauth_device_flow.py apps/control-plane/tests/test_oauth_device_flow.py
git commit -m "feat(auth): persist device authorization state"
```

## Task 3: Implement B device-code endpoints and Web approval reuse

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/auth/oauth.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/oauth.py`
- Test: `apps/control-plane/tests/test_oauth_device_flow.py`
- Test: existing Web OAuth API tests

- [ ] **Step 1: Add failing API tests**

Test `POST /api/v1/oauth/device/code` returns the six response fields, applies the configured TTL and
interval, and does not require the admin token. Test token polling returns `authorization_pending` before
approval, `access_denied` after denial, `expired_token` after expiry, and the native credential after
approval. Reuse the existing Web C decision endpoint to approve the same transaction and require TOTP when
the server setting is enabled.

- [ ] **Step 2: Run the focused API tests and verify failure**

```bash
uv run pytest apps/control-plane/tests/test_oauth_device_flow.py apps/control-plane/tests/test_oauth_api.py -q
```

- [ ] **Step 3: Implement endpoint/state behavior**

Create the device transaction from the shared authorization request. Return the configured verification
URI derived from `TERMFLOW_PUBLIC_BASE_URL`; never construct it from an untrusted request Host header. Add
the device grant branch to `/api/v1/oauth/token`, enforce the minimum polling interval, map all device
errors to stable JSON error kinds, and reuse existing DPoP/token issuance code.

- [ ] **Step 4: Verify rate limits and security regressions**

```bash
uv run pytest apps/control-plane/tests/test_oauth_device_flow.py apps/control-plane/tests/test_oauth_api.py apps/control-plane/tests/test_rate_limits.py -q
```

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/auth/oauth.py apps/control-plane/src/termflow_control_plane/api/oauth.py apps/control-plane/tests/test_oauth_device_flow.py
git commit -m "feat(auth): add OAuth device authorization endpoints"
```

## Task 4: Add Web C device approval page

**Files:**
- Create: `packages/client-ui/src/views/DeviceAuthorizeView.vue`
- Create: `packages/client-ui/src/views/DeviceAuthorizeView.test.ts`
- Modify: `packages/client-ui/src/router/routes.ts`
- Modify: existing OAuth/security API module and its tests
- Reuse: `packages/client-ui/src/components/common/ThemedQrCode.vue` for the Tauri device-code display;
  the Web approval page does not need to display a second QR code.

- [ ] **Step 1: Write failing view tests**

Cover empty/invalid/expired code, unauthenticated redirect to the existing Web C login, request preview,
approve, deny, TOTP challenge, and success confirmation. The preview must show client name/platform/version
and must never render an admin token or raw `device_code`.

- [ ] **Step 2: Run the tests and verify failure**

```bash
npm exec vitest run packages/client-ui/src/views/DeviceAuthorizeView.test.ts
```

- [ ] **Step 3: Implement the page and route**

Read `code` from the query string, allow manual `ABCD-EFGH` entry, call the preview endpoint, use the
existing admin session/TOTP components, and call the existing authorization decision endpoint. On success,
show “授权已完成，可以返回客户端”; never expose device secrets.

- [ ] **Step 4: Run UI tests and typecheck**

```bash
npm exec vitest run packages/client-ui/src/views/DeviceAuthorizeView.test.ts
npm run --workspace @termflow/client-ui typecheck
```

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/views/DeviceAuthorizeView.vue packages/client-ui/src/views/DeviceAuthorizeView.test.ts packages/client-ui/src/router/routes.ts packages/client-ui/src/api
git commit -m "feat(web): add device authorization approval page"
```

## Task 5: Add client-core device authorization polling

**Files:**
- Create: `packages/client-core/src/auth/deviceAuthorization.ts`
- Create: `packages/client-core/src/auth/deviceAuthorization.test.ts`
- Modify: `packages/client-core/src/api/oauth.ts`
- Modify: `packages/client-core/src/index.ts`

- [ ] **Step 1: Write failing state-machine tests**

Use a fake clock and injected request function. Assert that polling waits for `interval`, increases the
interval after `slow_down`, stops on `access_denied`/`expired_token`, can be cancelled, and stores no
credential until a successful response.

- [ ] **Step 2: Run the tests and verify failure**

```bash
npm exec vitest run packages/client-core/src/auth/deviceAuthorization.test.ts
```

- [ ] **Step 3: Implement the minimal polling session**

Expose `createDeviceAuthorization` and `pollDeviceAuthorization` through the existing OAuth API port.
Keep transport, clock/sleep and credential vault injected so tests do not wait in real time. Reuse the
existing native DPoP key and token-session credential type.

- [ ] **Step 4: Run core auth tests**

```bash
npm exec vitest run packages/client-core/src/auth/deviceAuthorization.test.ts packages/client-core/src/auth/nativeAuthorization.test.ts packages/client-core/src/auth/tokenSession.test.ts
npm run --workspace @termflow/client-core typecheck
```

- [ ] **Step 5: Commit**

```bash
git add packages/client-core/src/auth/deviceAuthorization.ts packages/client-core/src/auth/deviceAuthorization.test.ts packages/client-core/src/api/oauth.ts packages/client-core/src/index.ts
git commit -m "feat(auth): add device authorization polling"
```

## Task 6: Integrate the two paths into Tauri C

**Files:**
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue`
- Create: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`
- Create: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.ts`
- Create: `apps/clients/tauri/src/adapters/tauriDeviceAuthorization.ts`
- Modify: `apps/clients/tauri/src/adapters/tauriAuthorization.ts` for shared key/credential wiring

- [ ] **Step 1: Write failing UI/integration tests**

Assert the login page renders both buttons, browser mode calls `authorizeNativeClient`, device mode does
not call `openUrl` or `onOpenUrl`, device mode displays the returned user code/verification URI, polls
through pending/slow-down/success, and returns to the dashboard with the same credential type.

- [ ] **Step 2: Run the tests and verify failure**

```bash
npm exec vitest run apps/clients/tauri/src/views/NativeConnectView.test.ts apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts
```

- [ ] **Step 3: Implement the two-entry UI and device adapter**

Keep browser authorization as the default button. The secondary device button starts the device session,
shows a copyable code and themed QR that contains only `verification_uri_complete`, and exposes cancel and
regenerate actions. Store the device code/verifier only in memory. Map polling errors to actionable Chinese
messages and emit structured diagnostics without secrets.

- [ ] **Step 4: Verify Tauri frontend gates**

```bash
npm exec vitest run apps/clients/tauri/src/views/NativeConnectView.test.ts apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts apps/clients/tauri/src/adapters/tauriAuthorization.test.ts
npm run --workspace @termflow/tauri-client typecheck
```

- [ ] **Step 5: Commit**

```bash
git add apps/clients/tauri/src apps/clients/tauri/src-tauri/capabilities
git commit -m "feat(tauri): support browser and device authorization"
```

## Task 7: Independent Web C/Tauri E2E and operational documentation

**Files:**
- Create: `tests/e2e/test_device_authorization.py`
- Modify: `docs/web-client.md`
- Modify: `docs/operations.md`
- Modify: `docs/github-actions.md`
- Modify: `tests/docs/test_documentation_contract.py` for the dual-flow documentation contract

- [ ] **Step 1: Write the independent-browser E2E test**

Start an isolated B fixture, create a native device request, use an independent authenticated Web C client
to preview and approve it, then poll from the native side until token exchange succeeds. Include a second
run where the Web client denies the request and a third expired-code run.

- [ ] **Step 2: Run the E2E test and verify failure before implementation is complete**

```bash
uv run pytest tests/e2e/test_device_authorization.py -q
```

- [ ] **Step 3: Document both user paths and troubleshooting**

Document the two Tauri buttons, cross-device code/QR workflow, 15-minute expiry, polling behavior, the fact
that admin credentials remain in Web C, and the precise Windows client log path. State that the old Windows
installer must be replaced after native authorization changes.

- [ ] **Step 4: Run the full relevant verification**

```bash
uv run pytest apps/control-plane/tests/test_oauth_device_flow.py apps/control-plane/tests/test_oauth_api.py tests/e2e/test_device_authorization.py -q
npm exec vitest run packages/client-core/src/auth packages/client-ui/src/views apps/clients/tauri/src
npm run --workspace @termflow/client-ui typecheck
npm run --workspace @termflow/tauri-client typecheck
docker compose --env-file .env.example -f deploy/compose.yaml config --quiet
git diff --check
```

- [ ] **Step 5: Commit documentation and test updates**

```bash
git add tests/e2e docs/web-client.md docs/operations.md docs/github-actions.md tests/docs/test_documentation_contract.py scripts/run-web-e2e.sh
git commit -m "docs(auth): document dual native authorization"
```

## Final verification and release handoff

- [ ] Run `scripts/verify.sh` and record any environment-only failures separately from code failures.
- [ ] Push the branch and run the Windows package workflow to produce a new NSIS installer; local WSL tests
  cannot prove a Windows package exists.
- [ ] Install the new Windows package, exercise both browser and cross-device paths, and inspect
  `%LOCALAPPDATA%\\io.termflow.client\\logs\\termflow-client.log` for sanitized events.
- [ ] Do not call the feature complete until the independent Web C browser E2E and the rebuilt Windows
  installer both pass.
