# Compose Environment Cleanup and TOTP Setup-Key Popover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the default Compose deployment build the current Fork without a registry-specific image variable, simplify same-origin configuration, document every exposed environment knob, and show the manual TOTP setup key in a compact anchored popover.

**Architecture:** `deploy/compose.yaml` becomes the source-build authority and release-image verification uses `docker run` so image distribution remains independent from runtime configuration. B keeps its existing `public_base_url` fallback for browser Origin checks and its explicit-over-auto TOTP master-key priority. A focused Vue `SetupKeyPopover` owns disclosure accessibility and dismissal while `TotpActivationView` continues to own API and clipboard state.

**Tech Stack:** Docker Compose, Bash, GitHub Actions contracts, Python/pytest/PyYAML, FastAPI/Pydantic Settings, Vue 3, TypeScript, Vitest, Vue Test Utils, CSS, Playwright.

---

## File responsibility map

- `deploy/compose.yaml`: default source build and runtime wiring.
- `deploy/compose.dev.yaml`: remove after its build responsibility moves into the default file.
- `scripts/release/verify_control_plane_release_image.sh`: smoke an already-built image without runtime Compose image configuration.
- `scripts/verify.sh`: validate source-build Compose without `TERMFLOW_IMAGE`.
- `.env.example`: operator-facing runtime configuration with Chinese explanations.
- `README.md`, `docs/operations.md`, `docs/troubleshooting.md`: source-build deployment and rollback instructions.
- `packages/client-ui/src/components/settings/SetupKeyPopover.vue`: anchored non-modal setup-key disclosure.
- `packages/client-ui/src/components/settings/SetupKeyPopover.test.ts`: disclosure, copy, dismissal, and focus contracts.
- `packages/client-ui/src/views/TotpActivationView.vue`: integrate the popover into TOTP setup.
- `packages/client-ui/src/styles/app.css`: popover positioning and viewport containment.
- `apps/clients/web/e2e/settings-auth.spec.ts`: real-browser geometry and TOTP lifecycle coverage.
- `tests/deploy/test_compose_contract.py`, `tests/docs/test_documentation_contract.py`: deployment and documentation contracts.

### Task 1: Make default Compose build the current checkout

**Files:**
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `deploy/compose.yaml`
- Delete: `deploy/compose.dev.yaml`
- Modify: `scripts/release/verify_control_plane_release_image.sh`
- Modify: `scripts/verify.sh`

- [ ] **Step 1: Replace release-image Compose assertions with source-build contracts**

Replace the first two deployment tests and update verifier assertions:

```python
def test_default_compose_builds_current_checkout_without_an_image_source() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert service["build"] == {
        "context": "..",
        "dockerfile": "deploy/Dockerfile.control-plane",
    }
    assert "image" not in service
    assert "TERMFLOW_IMAGE" not in Path("deploy/compose.yaml").read_text()
    assert not Path("deploy/compose.dev.yaml").exists()


def test_release_image_smoke_is_independent_from_runtime_compose() -> None:
    verifier = Path("scripts/release/verify_control_plane_release_image.sh").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()
    assert 'IMAGE="$1"' in verifier
    assert "docker run --detach" in verifier
    assert "docker volume create" in verifier
    assert "docker rm --force" in verifier
    assert "docker volume rm" in verifier
    assert "TERMFLOW_IMAGE" not in verifier
    assert "docker compose" not in verifier
    assert "http://127.0.0.1:18076/healthz" in verifier
    assert "verify_control_plane_release_image.sh termflow-control-plane:ci" in workflow


def test_full_verification_checks_source_build_compose_configuration() -> None:
    verify = Path("scripts/verify.sh").read_text()
    assert "TERMFLOW_IMAGE" not in verify
    assert 'TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough"' in verify
    assert "docker compose -f deploy/compose.yaml config --quiet" in verify
```

- [ ] **Step 2: Run the deployment contracts and verify RED**

Run:

```bash
uv run --no-cache --frozen --package termflow-control-plane \
  pytest -q tests/deploy/test_compose_contract.py
```

Expected: FAIL because Compose still requires `TERMFLOW_IMAGE`, the development override exists, and the verifier exports the image variable.

- [ ] **Step 3: Move the source build into default Compose**

Replace `image` in `deploy/compose.yaml` with:

```yaml
    build:
      context: ..
      dockerfile: deploy/Dockerfile.control-plane
```

Delete `deploy/compose.dev.yaml`; it no longer owns distinct behavior.

- [ ] **Step 4: Verify the caller-supplied release image directly**

Keep the verifier argument guard, then replace Compose orchestration with an isolated container and volume:

```bash
IMAGE="$1"
CONTAINER="termflow-release-image-test-$$"
VOLUME="${CONTAINER}-data"
HOST_PORT="18076"

cleanup() {
  docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "${VOLUME}" >/dev/null
docker run --detach \
  --name "${CONTAINER}" \
  --pull never \
  --publish "127.0.0.1:${HOST_PORT}:8000" \
  --mount "source=${VOLUME},target=/app/data" \
  --env TERMFLOW_ADMIN_TOKEN=release-test-admin-token-which-is-long-enough \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK=true \
  --env "TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:${HOST_PORT}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/data/totp-master-key \
  "${IMAGE}" >/dev/null

for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error "http://127.0.0.1:${HOST_PORT}/healthz"; then
    exit 0
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}")" != "true" ]]; then
    docker logs "${CONTAINER}" >&2
    exit 1
  fi
  sleep 1
done
docker logs "${CONTAINER}" >&2
echo "control-plane release image did not become healthy" >&2
exit 1
```

The script must not contain a Registry, repository owner, or default image; the caller supplies an image it already built.

- [ ] **Step 5: Remove `TERMFLOW_IMAGE` from full verification**

Replace the Compose config block in `scripts/verify.sh` with:

```bash
TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough" \
  docker compose -f deploy/compose.yaml config --quiet
```

Keep `CONTROL_PLANE_IMAGE` only for explicit image build and content-check commands.

- [ ] **Step 6: Run deployment contracts and shell syntax checks**

```bash
uv run --no-cache --frozen --package termflow-control-plane \
  pytest -q tests/deploy/test_compose_contract.py
bash -n scripts/release/verify_control_plane_release_image.sh scripts/verify.sh
TERMFLOW_ADMIN_TOKEN=verify-admin-token-that-is-long-enough \
  docker compose -f deploy/compose.yaml config --quiet
```

Expected: tests PASS and both shell/Compose checks exit 0 without `TERMFLOW_IMAGE`.

- [ ] **Step 7: Commit**

```bash
git add deploy/compose.yaml deploy/compose.dev.yaml \
  scripts/release/verify_control_plane_release_image.sh scripts/verify.sh \
  tests/deploy/test_compose_contract.py
git commit -m "fix(deploy): build the current checkout by default"
```

### Task 2: Simplify and document operator environment configuration

**Files:**
- Modify: `tests/docs/test_documentation_contract.py`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `.env.example`
- Modify: `deploy/compose.yaml`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/troubleshooting.md`

- [ ] **Step 1: Write failing environment/documentation contracts**

Update the operator-document test with:

```python
assert "TERMFLOW_IMAGE" not in env_example
assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in env_example
assert "TERMFLOW_PUBLIC_BASE_URL" in env_example
assert "# TERMFLOW_TOTP_MASTER_KEY=replace-with-generated-base64url-key" in env_example
for explanation in (
    "8 小时", "浏览器会话", "一次性注册码", "64 KiB",
    "256 KiB/s", "256 条", "1 MiB", "30 秒",
):
    assert explanation in env_example
assert "docker compose --env-file .env -f deploy/compose.yaml up -d --build" in operations
assert "TERMFLOW_IMAGE" not in readme
assert "TERMFLOW_IMAGE" not in troubleshooting
```

Update the Compose environment contract:

```python
assert "TERMFLOW_PUBLIC_BASE_URL" in environment
assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in environment
assert environment["TERMFLOW_TOTP_MASTER_KEY"] is None
```

- [ ] **Step 2: Run focused contracts and verify RED**

```bash
uv run --no-cache --frozen --package termflow-control-plane pytest -q \
  tests/docs/test_documentation_contract.py \
  tests/deploy/test_compose_contract.py \
  apps/control-plane/tests/test_config.py \
  apps/control-plane/tests/test_browser_sessions.py
```

Expected: documentation and Compose assertions FAIL while existing B fallback/security tests remain PASS.

- [ ] **Step 3: Rewrite `.env.example` as an operator reference**

Use this exact organization and retain Compose defaults:

```dotenv
# 必填。B 的初始管理员凭据，至少包含 32 个 UTF-8 字节。
# 生成：python -c 'import secrets; print(secrets.token_urlsafe(32))'
TERMFLOW_ADMIN_TOKEN=replace-with-generated-secret

# 可选。宿主机 loopback 监听端口，默认 8765；仅在端口冲突时修改。
TERMFLOW_HOST_PORT=8765
# 必填于反向代理部署。用户、Computer A、App 和 EXE 实际访问的外部服务网址。
# 本地保留 loopback；生产替换为不带路径的公网 HTTPS origin。
TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:8765
# 仅允许 loopback HTTP 使用非 Secure 浏览器 Cookie；公网 HTTPS 应设为 false。
TERMFLOW_ALLOW_INSECURE_LOOPBACK=true

# 可选。单 B 默认在 termflow-data 中自动创建并复用私有主密钥。
# 仅在多 B 共享密钥或明确管理密钥材料时设置；完成 2FA 绑定后不可直接更换。
# 生成：python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
# TERMFLOW_TOTP_MASTER_KEY=replace-with-generated-base64url-key

# 浏览器 HttpOnly 会话有效期，单位秒；默认 28800，即 8 小时。
TERMFLOW_BROWSER_SESSION_TTL_SECONDS=28800
# B 内存中同时保留的浏览器会话上限，默认 4096；大量并发管理员会话时才调整。
TERMFLOW_BROWSER_SESSION_CAPACITY=4096
# 添加电脑生成的一次性注册码有效期，单位秒；默认 60，范围 10–600。
TERMFLOW_ENROLLMENT_TOKEN_TTL_SECONDS=60

# 单个浏览器终端输入帧上限，单位字节；默认 65536，即 64 KiB。
TERMFLOW_TERMINAL_MAX_FRAME_BYTES=65536
# 单个终端输入速率上限，单位字节/秒；默认 262144，即 256 KiB/s。
TERMFLOW_TERMINAL_INPUT_RATE_BYTES_PER_SECOND=262144
# 单个终端待发送队列消息上限，默认 256 条；过小会更早断开慢客户端。
TERMFLOW_TERMINAL_QUEUE_MAX_MESSAGES=256
# 单个终端待发送队列总字节上限，默认 1048576，即 1 MiB。
TERMFLOW_TERMINAL_QUEUE_MAX_BYTES=1048576
# 临时断网后允许原终端会话恢复的宽限期，单位秒；默认 30 秒。
TERMFLOW_TERMINAL_RESUME_GRACE_SECONDS=30
```

Do not add image sources, duplicate trusted Origins, database paths, static paths, or the automatic key-file path.

- [ ] **Step 4: Remove the duplicate Origin from ordinary Compose**

Delete `TERMFLOW_TRUSTED_WEB_ORIGINS` from `deploy/compose.yaml`. Keep `TERMFLOW_TOTP_MASTER_KEY:` and `TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE` unchanged. Do not modify `Settings.allowed_web_origins`; its existing `public_base_url` fallback remains authoritative.

- [ ] **Step 5: Align deployment and rollback documentation**

Use this normal workflow in README and operations:

```bash
cp .env.example .env
# 编辑 TERMFLOW_ADMIN_TOKEN 和实际的 TERMFLOW_PUBLIC_BASE_URL。
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Describe rollback as checking out the previously verified source tag/commit and rerunning `up -d --build` without deleting `termflow-data`. Actions may build/publish artifacts, but base Compose must not select a Registry. Replace troubleshooting instructions that change `TERMFLOW_IMAGE`; preserve the warning against `down --volumes`.

- [ ] **Step 6: Run environment, Origin, and documentation contracts**

```bash
uv run --no-cache --frozen --package termflow-control-plane pytest -q \
  tests/docs/test_documentation_contract.py \
  tests/deploy/test_compose_contract.py \
  apps/control-plane/tests/test_config.py \
  apps/control-plane/tests/test_browser_sessions.py \
  apps/control-plane/tests/test_terminal_websocket.py
```

Expected: all selected tests PASS, including malicious-Origin rejection.

- [ ] **Step 7: Commit**

```bash
git add .env.example deploy/compose.yaml README.md docs/operations.md \
  docs/troubleshooting.md tests/docs/test_documentation_contract.py \
  tests/deploy/test_compose_contract.py
git commit -m "docs(deploy): clarify runtime environment settings"
```

### Task 3: Build the accessible setup-key popover component

**Files:**
- Create: `packages/client-ui/src/components/settings/SetupKeyPopover.vue`
- Create: `packages/client-ui/src/components/settings/SetupKeyPopover.test.ts`

- [ ] **Step 1: Write the failing component tests**

Cover closed state, dialog semantics, copy, repeated trigger, Escape, outside pointer, and focus restoration:

```ts
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import SetupKeyPopover from './SetupKeyPopover.vue'

afterEach(() => { document.body.innerHTML = '' })

describe('SetupKeyPopover', () => {
  it('opens a non-modal setup-key dialog and emits copy', async () => {
    const wrapper = mount(SetupKeyPopover, {
      attachTo: document.body,
      props: { setupKey: 'SETUPKEY', copied: false },
    })
    const trigger = wrapper.get('[data-action="toggle-setup-key"]')
    expect(trigger.attributes('aria-haspopup')).toBe('dialog')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    await trigger.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(dialog.attributes('aria-modal')).toBeUndefined()
    expect(dialog.get('[data-setup-key]').text()).toBe('SETUPKEY')
    await dialog.get('[data-action="copy-setup-key"]').trigger('click')
    expect(wrapper.emitted('copy')).toHaveLength(1)
    wrapper.unmount()
  })

  it('closes by trigger, Escape, and outside pointer and restores trigger focus', async () => {
    const wrapper = mount(SetupKeyPopover, {
      attachTo: document.body,
      props: { setupKey: 'SETUPKEY', copied: false },
    })
    const trigger = wrapper.get<HTMLButtonElement>('[data-action="toggle-setup-key"]')
    await trigger.trigger('click')
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    await trigger.trigger('click')
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    await trigger.trigger('click')
    await trigger.trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
```

- [ ] **Step 2: Run the component test and verify RED**

```bash
npm run test:run --workspace @termflow/client-ui -- SetupKeyPopover.test.ts
```

Expected: FAIL because `SetupKeyPopover.vue` does not exist.

- [ ] **Step 3: Implement the focused non-modal popover**

Create this public template contract:

```vue
<template>
  <div ref="root" class="setup-key-disclosure">
    <button
      ref="trigger"
      data-action="toggle-setup-key"
      class="setup-key-toggle"
      type="button"
      aria-haspopup="dialog"
      :aria-expanded="open"
      aria-controls="totp-setup-key-popover"
      @click="toggle"
    >无法扫描？使用设置密钥</button>
    <section
      v-if="open"
      id="totp-setup-key-popover"
      class="setup-key-popover"
      role="dialog"
      aria-labelledby="totp-setup-key-title"
      @keydown="onKeydown"
    >
      <h3 id="totp-setup-key-title">设置密钥</h3>
      <code data-setup-key>{{ setupKey }}</code>
      <button data-action="copy-setup-key" class="compact-secondary-button" type="button" @click="$emit('copy')">
        {{ copied ? '已复制' : '复制密钥' }}
      </button>
    </section>
  </div>
</template>
```

Define `setupKey: string` and `copied: boolean` props plus a typed `copy` event. Keep `open` private. Register a document `pointerdown` listener in `onMounted`, remove it in `onBeforeUnmount`, and close only when the target is outside `root`. Implement a shared `close()` that clears `open` and focuses `trigger` on `nextTick`; use it for Escape, outside pointer, and the second trigger click.

- [ ] **Step 4: Run component tests and typecheck**

```bash
npm run test:run --workspace @termflow/client-ui -- SetupKeyPopover.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: component tests PASS and Vue typecheck exits 0.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/components/settings/SetupKeyPopover.vue \
  packages/client-ui/src/components/settings/SetupKeyPopover.test.ts
git commit -m "feat(ui): add TOTP setup key popover"
```

### Task 4: Integrate and contain the setup-key popover

**Files:**
- Modify: `packages/client-ui/src/views/TotpActivationView.test.ts`
- Modify: `packages/client-ui/src/views/TotpActivationView.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/e2e/settings-auth.spec.ts`

- [ ] **Step 1: Update the view test before production code**

After setup creation, assert through the full view:

```ts
const disclosure = wrapper.get('[data-action="toggle-setup-key"]')
expect(disclosure.attributes('aria-haspopup')).toBe('dialog')
expect(disclosure.attributes('aria-expanded')).toBe('false')
expect(wrapper.find('[data-setup-key]').exists()).toBe(false)
await disclosure.trigger('click')
const setupKeyDialog = wrapper.get('[role="dialog"]')
expect(disclosure.attributes('aria-expanded')).toBe('true')
expect(setupKeyDialog.classes()).toContain('setup-key-popover')
expect(setupKeyDialog.get('[data-setup-key]').text()).toBe('SETUPKEY')
await setupKeyDialog.get('[data-action="copy-setup-key"]').trigger('click')
await flushPromises()
expect(writeText).toHaveBeenCalledWith('SETUPKEY')
expect(setupKeyDialog.text()).toContain('已复制')
```

- [ ] **Step 2: Add the responsive CSS contract and verify RED**

```ts
expect(appCss).toContain('.setup-key-disclosure { position: relative;')
expect(appCss).toContain('.setup-key-popover { position: absolute;')
expect(appCss).toContain('width: min(18rem, calc(100vw - 2 * var(--space-4)));')
expect(appCss).toContain('z-index: 20;')
expect(appCss).not.toContain('.setup-key-panel')
```

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  TotpActivationView.test.ts responsive-contract.test.ts
```

Expected: FAIL because the view still uses an in-flow `.setup-key-panel`.

- [ ] **Step 3: Replace inline state with `SetupKeyPopover`**

Use:

```vue
<SetupKeyPopover
  :setup-key="setup.setup_key"
  :copied="setupKeyCopied"
  @copy="copySetupKey"
/>
```

Import the component. Remove `setupKeyExpanded` and all assignments to it. Keep `setupKeyCopied`, resetting it on setup creation and confirmation. Keep `copySetupKey()` on the parent so clipboard access remains injected through `ClientRuntime`.

- [ ] **Step 4: Replace flow-layout CSS with anchored CSS**

Remove `.setup-key-panel` styles and add:

```css
.setup-key-disclosure { position: relative; display: grid; justify-items: center; }
.setup-key-popover { position: absolute; z-index: 20; inset-block-start: calc(100% + var(--space-2)); inset-inline-start: 50%; width: min(18rem, calc(100vw - 2 * var(--space-4))); display: grid; gap: var(--space-2); transform: translateX(-50%); padding: var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-surface); box-shadow: var(--shadow-panel); text-align: start; }
.setup-key-popover h3 { margin: 0; font-size: 0.9rem; }
.setup-key-popover code { display: block; min-width: 0; overflow-wrap: anywhere; padding: var(--space-2); border: 1px solid var(--color-border); border-radius: var(--radius-sm); background: var(--color-elevated); font-family: var(--font-mono); font-size: 0.76rem; }
.setup-key-popover .compact-secondary-button { justify-self: stretch; }
```

Use the existing theme-provided `--shadow-panel` token; do not add a new shadow token.

- [ ] **Step 5: Run focused unit and type contracts**

```bash
npm run test:run --workspace @termflow/client-ui -- \
  SetupKeyPopover.test.ts TotpActivationView.test.ts responsive-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: focused tests PASS and typecheck exits 0.

- [ ] **Step 6: Update real-browser geometry assertions**

Capture QR and confirmation-form rectangles before opening. Open the disclosure and assert:

```ts
const setupKeyDialog = page.getByRole('dialog', { name: '设置密钥' })
await expect(setupKeyDialog).toBeVisible()
const geometry = await setupKeyDialog.evaluate((dialog) => {
  const box = dialog.getBoundingClientRect()
  const trigger = document.querySelector<HTMLElement>('[data-action="toggle-setup-key"]')!.getBoundingClientRect()
  return { left: box.left, right: box.right, top: box.top, triggerBottom: trigger.bottom, viewportWidth: window.innerWidth }
})
expect(geometry.left).toBeGreaterThanOrEqual(0)
expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth)
expect(geometry.top).toBeGreaterThanOrEqual(geometry.triggerBottom)
```

Assert the QR/form `x`, `y`, `width`, and `height` remain unchanged within one pixel. Read the setup key inside the dialog for existing TOTP generation, close with Escape, and assert the trigger regains focus.

- [ ] **Step 7: Run isolated authenticated browser flow**

```bash
./scripts/run-web-e2e.sh apps/clients/web/e2e/settings-auth.spec.ts
```

Expected: desktop security trajectory PASS, screenshot shows a compact popover near the trigger, and console errors remain empty. Inspect the exact emitted temporary evidence directory before removing only that directory.

- [ ] **Step 8: Commit**

```bash
git add packages/client-ui/src/views/TotpActivationView.vue \
  packages/client-ui/src/views/TotpActivationView.test.ts \
  packages/client-ui/src/styles/app.css \
  packages/client-ui/src/test/responsive-contract.test.ts \
  apps/clients/web/e2e/settings-auth.spec.ts
git commit -m "fix(ui): show TOTP setup key in an anchored popover"
```

### Task 5: Complete regression and image verification

**Files:**
- Verify only unless an in-scope failing contract requires a minimal fix.

- [ ] **Step 1: Run all JavaScript tests, types, and Web build**

```bash
npm run test:run
npm run typecheck
npm run build:web
```

Expected: every workspace Vitest suite PASS, all TypeScript/Vue projects typecheck, and Vite produces `apps/clients/web/dist`.

- [ ] **Step 2: Run all Python checks**

```bash
uv run --no-cache --frozen --all-packages pytest -q
uv run --no-cache --frozen --all-packages ruff check .
uv run --no-cache --frozen --all-packages mypy \
  packages/protocol/src apps/control-plane/src apps/node/src
```

Expected: Python tests, lint, and type checks PASS.

- [ ] **Step 3: Build and verify the source image**

```bash
scripts/build-control-plane-image.sh termflow-control-plane:env-popover-verify
scripts/verify-control-plane-image.sh termflow-control-plane:env-popover-verify
scripts/release/verify_control_plane_release_image.sh \
  termflow-control-plane:env-popover-verify
```

Expected: the image contains B and latest Web C but no source/toolchain, then becomes healthy in the independent release-image verifier.

- [ ] **Step 4: Verify default Compose without image configuration**

Create a temporary env file outside the repository containing a generated test admin token and loopback `TERMFLOW_PUBLIC_BASE_URL`, then run:

```bash
docker compose --env-file "$TEMP_ENV" -f deploy/compose.yaml config --quiet
docker compose --env-file "$TEMP_ENV" -f deploy/compose.yaml build control-plane
```

Expected: both commands exit 0 without an image source, Registry, `TERMFLOW_IMAGE`, or `TERMFLOW_TRUSTED_WEB_ORIGINS`.

- [ ] **Step 5: Review final diff and repository state**

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Expected: no whitespace errors, only intentional changes, and all task commits present. Do not push or merge without the user's explicit finishing choice.
