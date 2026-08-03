# TermFlow Settings UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Center theme choices, make the relay URL a coherent field, prevent help-tooltip overflow, place authenticator reconfiguration in the selected header layout, and remove nonessential security implementation copy.

**Architecture:** Keep all behavior in the existing shared Vue UI package. Introduce one small `ContextHelp` presentation component around the existing Lucide `CircleHelp` pattern, then compose it from TOTP views; API calls, routes, and runtime ports remain unchanged. CSS stays in the existing shared stylesheet, with component and responsive-contract tests locking down the spatial rules.

**Tech Stack:** Vue 3, TypeScript, Vue Test Utils, Vitest/jsdom, Lucide Vue, Playwright, shared design tokens.

---

## File map

- Create `packages/client-ui/src/components/common/ContextHelp.vue`: reusable accessible `?` trigger and tooltip text.
- Create `packages/client-ui/src/components/common/ContextHelp.test.ts`: focus/ARIA contract for contextual help.
- Modify `packages/client-ui/src/components/settings/ThemePicker.test.ts`: settings-only centering contract.
- Modify `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`: coherent service-URL field and description-free QR dialog.
- Modify `packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts`: field hierarchy and concise QR assertions.
- Modify `packages/client-ui/src/components/common/QrCodeDialog.vue`: make descriptive copy optional.
- Modify `packages/client-ui/src/components/common/QrCodeDialog.test.ts`: verify both described and concise dialogs.
- Modify `packages/client-ui/src/components/settings/TotpProtectionLabel.vue`: use shared contextual help.
- Modify `packages/client-ui/src/components/settings/TotpPanel.vue`: selected header action group and concise onboarding.
- Modify `packages/client-ui/src/components/settings/TotpPanel.test.ts`: onboarding, header placement, and reconfiguration behavior.
- Modify `packages/client-ui/src/views/TotpActivationView.vue`: remove implementation copy and use the selected header action treatment.
- Modify `packages/client-ui/src/views/TotpActivationView.test.ts`: necessary-copy and action-placement contract.
- Modify `packages/client-ui/src/components/settings/TotpProtectionDialog.vue`: remove redundant form instructions.
- Modify `packages/client-ui/src/components/settings/TotpProtectionDialog.test.ts`: concise-dialog contract.
- Modify `packages/client-ui/src/styles/app.css`: centering, field grouping, header actions, and bounded tooltips.
- Modify `packages/client-ui/src/test/responsive-contract.test.ts`: tooltip containment and no-horizontal-overflow CSS contract.
- Modify `apps/clients/web/e2e/settings-auth.spec.ts`: real-browser geometry and concise-copy assertions.

### Task 1: Center theme choices and group the relay URL field

**Files:**
- Modify: `packages/client-ui/src/components/settings/ThemePicker.test.ts`
- Modify: `packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts`
- Modify: `packages/client-ui/src/components/common/QrCodeDialog.test.ts`
- Modify: `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`
- Modify: `packages/client-ui/src/components/common/QrCodeDialog.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing theme, field-hierarchy, and optional-description tests**

Add this assertion to the existing full-width theme test:

```ts
expect(css).toContain('.settings-page .theme-option { width: 100%; justify-content: center; }')
```

Extend `ServerConnectionPanel.test.ts` after the heading assertions:

```ts
const field = wrapper.get('[data-server-field]')
expect(field.get('[data-server-label] h3').text()).toBe('服务网址')
expect(field.get('.server-address-row [data-server-issuer]').text()).toBe('https://relay.example.com')
expect(field.element.children[0]?.classList).toContain('server-field-heading')
expect(field.element.children[1]?.classList).toContain('server-address-row')
```

After opening the server QR dialog, assert that no implementation-description paragraph is rendered:

```ts
const dialog = wrapper.get('[role="dialog"]')
expect(dialog.find('p').exists()).toBe(false)
expect(dialog.attributes('aria-describedby')).toBeUndefined()
```

Add a second `QrCodeDialog.test.ts` case:

```ts
it('omits description markup when concise content needs no explanation', async () => {
  const wrapper = mount(QrCodeDialog, {
    props: { open: true, title: '服务网址二维码', value: 'termflow://relay' },
    global: { plugins: [createClientUi(createFakeRuntime())] },
  })
  await flushPromises()
  const dialog = wrapper.get('[role="dialog"]')
  expect(dialog.find('p').exists()).toBe(false)
  expect(dialog.attributes('aria-describedby')).toBeUndefined()
})
```

- [ ] **Step 2: Run the targeted tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/components/settings/ThemePicker.test.ts \
  src/components/settings/ServerConnectionPanel.test.ts \
  src/components/common/QrCodeDialog.test.ts
```

Expected: FAIL because theme options lack `justify-content: center`, no `data-server-field` wrapper exists, and `description` is still required/rendered.

- [ ] **Step 3: Implement centered choices, a grouped field, and optional QR copy**

Wrap the service heading and address row in `ServerConnectionPanel.vue`:

```vue
<div data-server-field class="server-field">
  <div data-server-label class="server-field-heading">
    <h3 id="server-url-label">服务网址</h3>
    <button
      ref="qrTrigger"
      data-action="show-server-qr"
      class="icon-button icon-only"
      type="button"
      aria-label="显示服务网址二维码"
      @click="qrOpen = true"
    >
      <QrCode :size="18" aria-hidden="true" />
    </button>
  </div>
  <div class="server-address-row" aria-labelledby="server-url-label">
    <code data-server-issuer>{{ issuer }}</code>
    <button data-action="copy-server-url" class="secondary-button" type="button" @click="copyIssuer">
      {{ copied ? '已复制' : '复制' }}
    </button>
  </div>
</div>
```

Call `QrCodeDialog` without the old technical `description` prop. In `QrCodeDialog.vue`, make the prop optional and conditionally wire its semantics:

```vue
<section
  ref="panel"
  class="dialog-panel qr-dialog-panel"
  role="dialog"
  aria-modal="true"
  :aria-labelledby="titleId"
  :aria-describedby="description ? descriptionId : undefined"
>
  <!-- existing heading and QR -->
  <p v-if="description" :id="descriptionId">{{ description }}</p>
</section>
```

```ts
const props = defineProps<{
  open: boolean
  title: string
  value: string
  description?: string
  returnFocus?: HTMLElement | null
}>()
```

Update `app.css`:

```css
.settings-page .theme-option { width: 100%; justify-content: center; }
.server-field { display: grid; gap: var(--space-2); }
.server-field-heading { min-height: 2.75rem; display: flex; align-items: center; gap: var(--space-2); }
```

Remove the obsolete `.server-url-heading` rule.

- [ ] **Step 4: Run the targeted tests and verify GREEN**

Run the command from Step 2.

Expected: all targeted tests PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add packages/client-ui/src/components/settings/ThemePicker.test.ts \
  packages/client-ui/src/components/settings/ServerConnectionPanel.vue \
  packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts \
  packages/client-ui/src/components/common/QrCodeDialog.vue \
  packages/client-ui/src/components/common/QrCodeDialog.test.ts \
  packages/client-ui/src/styles/app.css
git commit -m "fix(ui): center themes and group server URL"
```

### Task 2: Add accessible contextual help and contain its tooltip

**Files:**
- Create: `packages/client-ui/src/components/common/ContextHelp.vue`
- Create: `packages/client-ui/src/components/common/ContextHelp.test.ts`
- Modify: `packages/client-ui/src/components/settings/TotpProtectionLabel.vue`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing component and CSS containment tests**

Create `ContextHelp.test.ts`:

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ContextHelp from './ContextHelp.vue'

describe('ContextHelp', () => {
  it('connects one question-mark button to wrapping tooltip copy', async () => {
    const wrapper = mount(ContextHelp, {
      props: { label: '说明启用双重认证登录', text: '新的登录需要一次性验证码。' },
    })
    const trigger = wrapper.get('button')
    const tooltip = wrapper.get('[role="tooltip"]')
    expect(trigger.attributes('aria-label')).toBe('说明启用双重认证登录')
    expect(trigger.attributes('aria-describedby')).toBe(tooltip.attributes('id'))
    expect(tooltip.text()).toBe('新的登录需要一次性验证码。')
    expect(wrapper.find('svg').exists()).toBe(true)
  })
})
```

Replace the old positioning assertions in `responsive-contract.test.ts` with:

```ts
expect(appCss).toContain('.security-setting-row { position: relative;')
expect(appCss).toContain('.security-setting-row > .security-setting-label { position: static;')
expect(appCss).toContain(".security-setting-label .help-tooltip [role='tooltip'] { inset-inline: var(--space-3); width: auto; max-width: none; white-space: normal; }")
expect(appCss).toContain('.settings-panel-heading { position: relative;')
expect(appCss).toContain(".settings-panel-heading .help-tooltip [role='tooltip'] { inset-inline: 0; width: auto; white-space: normal; }")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/components/common/ContextHelp.test.ts \
  src/components/settings/TotpPanel.test.ts \
  src/test/responsive-contract.test.ts
```

Expected: FAIL because `ContextHelp.vue` does not exist and the tooltip is positioned from the label rather than the complete row.

- [ ] **Step 3: Implement `ContextHelp` and row-bounded positioning**

Create `ContextHelp.vue`:

```vue
<template>
  <span class="help-tooltip">
    <button
      class="icon-button icon-only help-tooltip-trigger"
      type="button"
      :aria-label="label"
      :aria-describedby="tooltipId"
    >
      <CircleHelp :size="17" aria-hidden="true" />
    </button>
    <span :id="tooltipId" role="tooltip">{{ text }}</span>
  </span>
</template>

<script setup lang="ts">
import { CircleHelp } from '@lucide/vue'

defineProps<{ label: string; text: string }>()
const tooltipId = `context-help-${crypto.randomUUID()}`
</script>
```

Refactor `TotpProtectionLabel.vue` to compose it:

```vue
<template>
  <div data-totp-protection-label class="security-setting-label">
    <strong>启用双重认证登录</strong>
    <ContextHelp
      data-action="explain-totp-protection"
      label="说明启用双重认证登录"
      text="启用后，新的管理员登录和客户端授权都需要验证器生成的一次性验证码。"
    />
  </div>
</template>

<script setup lang="ts">
import ContextHelp from '../common/ContextHelp.vue'
</script>
```

Vue component attributes fall through to the root `span`, preserving the existing `data-action` selector.

Update CSS so the row is the containing block:

```css
.security-setting-row { position: relative; display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); padding: var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-elevated); }
.security-setting-row > .security-setting-label { position: static; display: inline-flex; align-items: center; gap: var(--space-1); white-space: nowrap; }
.security-setting-label .help-tooltip [role='tooltip'] { inset-inline: var(--space-3); width: auto; max-width: none; white-space: normal; }
.settings-panel-heading { position: relative; display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.settings-panel-heading .help-tooltip [role='tooltip'] { inset-inline: 0; width: auto; white-space: normal; }
```

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.

Expected: all targeted tests PASS, including existing TOTP label behavior.

- [ ] **Step 5: Commit Task 2**

```bash
git add packages/client-ui/src/components/common/ContextHelp.vue \
  packages/client-ui/src/components/common/ContextHelp.test.ts \
  packages/client-ui/src/components/settings/TotpProtectionLabel.vue \
  packages/client-ui/src/test/responsive-contract.test.ts \
  packages/client-ui/src/styles/app.css
git commit -m "fix(ui): contain contextual help tooltips"
```

### Task 3: Put authenticator reconfiguration in the TOTP card header

**Files:**
- Modify: `packages/client-ui/src/components/settings/TotpPanel.test.ts`
- Modify: `packages/client-ui/src/components/settings/TotpPanel.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing concise-onboarding and header-action tests**

In the unconfigured test, add:

```ts
expect(wrapper.find('.settings-copy').exists()).toBe(false)
expect(wrapper.get('[data-action="explain-totp"] [role="tooltip"]').text()).toContain('一次性验证码')
```

Add a configured-state test:

```ts
it('places reconfiguration beside the bound status in the card header', async () => {
  const runtime = createFakeRuntime({
    api: { security: { totpStatus: vi.fn().mockResolvedValue({ configured: true, enabled: false, available: true }) } } as unknown as ClientRuntime['api'],
  })
  const { wrapper, router } = await mountPanel(runtime)
  const actions = wrapper.get('[data-authenticator-actions]')
  expect(actions.element.parentElement?.classList).toContain('settings-panel-heading')
  expect(actions.get('.status-chip').text()).toBe('验证器已绑定')
  expect(wrapper.find('.settings-panel > .settings-action-button').exists()).toBe(false)
  await actions.get('[data-action="reconfigure-totp"]').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/settings/two-factor-auth')
})
```

- [ ] **Step 2: Run the panel test and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/components/settings/TotpPanel.test.ts
```

Expected: FAIL because onboarding copy is inline and the reconfigure button is below the setting row.

- [ ] **Step 3: Implement concise onboarding and visual option B**

In the panel heading, add contextual help beside the title and the selected action group when configured:

```vue
<div class="settings-panel-heading">
  <div>
    <p class="eyebrow">Two Factor Authentication</p>
    <div class="settings-heading-label">
      <h2 id="totp-heading">双重因素认证</h2>
      <ContextHelp
        v-if="!loading && status.available && !status.configured"
        data-action="explain-totp"
        label="说明双重因素认证"
        text="绑定验证器 App 后，可为新的登录和客户端授权增加一次性验证码。"
      />
    </div>
  </div>
  <div v-if="!loading && status.configured" data-authenticator-actions class="settings-panel-heading-actions">
    <span class="status-chip" data-status="enabled">验证器已绑定</span>
    <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="activate">重新配置</button>
  </div>
  <span v-else-if="!loading" class="status-chip" data-status="disabled">未激活</span>
</div>
```

Remove the onboarding paragraph and the standalone reconfigure button. Keep the activation button and protection row unchanged.

Add CSS:

```css
.settings-heading-label, .settings-panel-heading-actions { display: flex; align-items: center; gap: var(--space-2); }
.compact-secondary-button { min-height: 2rem; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); background: var(--color-elevated); color: var(--color-text-primary); font-weight: 700; }
```

- [ ] **Step 4: Run the panel tests and verify GREEN**

Run the command from Step 2.

Expected: all `TotpPanel` tests PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add packages/client-ui/src/components/settings/TotpPanel.vue \
  packages/client-ui/src/components/settings/TotpPanel.test.ts \
  packages/client-ui/src/styles/app.css
git commit -m "fix(ui): move authenticator action into header"
```

### Task 4: Remove nonessential TOTP detail-page copy

**Files:**
- Modify: `packages/client-ui/src/views/TotpActivationView.test.ts`
- Modify: `packages/client-ui/src/views/TotpActivationView.vue`
- Modify: `packages/client-ui/src/components/settings/TotpProtectionDialog.test.ts`
- Modify: `packages/client-ui/src/components/settings/TotpProtectionDialog.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing concise-copy and configured-header tests**

After initial activation-view mounting, assert:

```ts
expect(wrapper.text()).not.toContain('管理员 Token 只用于本次验证，不会保存在客户端。')
expect(wrapper.text()).not.toContain('使用你的验证器 App 完成绑定')
```

After beginning setup, assert the manual key has a field heading and optional help rather than an explanatory paragraph:

```ts
expect(wrapper.get('[data-setup-key-label]').text()).toBe('设置密钥')
expect(wrapper.get('[data-action="explain-setup-key"] [role="tooltip"]').text()).toContain('无法扫码')
expect(wrapper.text()).not.toContain('在验证器 App 中扫码，或手工输入下面的设置密钥。')
```

After confirming setup, assert the detail-card action uses the same chosen placement:

```ts
const configuredHeading = wrapper.get('[data-configured-authenticator-heading]')
expect(configuredHeading.get('h2').text()).toBe('验证器已绑定')
expect(configuredHeading.get('[data-action="reconfigure-totp"]').text()).toBe('重新配置')
expect(wrapper.find('.totp-guide-card > .settings-action-button').exists()).toBe(false)
```

In `TotpProtectionDialog.test.ts`, assert the dialog does not repeat instructions already expressed by labels:

```ts
expect(wrapper.text()).not.toContain('请输入管理员 Token 和验证器刚生成的 6 位验证码。')
expect(wrapper.get('label[for="protection-admin-token"]').text()).toBe('管理员 Token')
expect(wrapper.get('label[for="protection-totp-code"]').text()).toBe('当前验证码')
```

- [ ] **Step 2: Run detail and dialog tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/views/TotpActivationView.test.ts \
  src/components/settings/TotpProtectionDialog.test.ts
```

Expected: FAIL on the old paragraphs, missing setup-key label/help, and standalone reconfigure button.

- [ ] **Step 3: Implement concise states and selected header placement**

Remove the introductory paragraph from `.totp-guide-heading-copy`. Replace setup copy with a labeled key and contextual help:

```vue
<div class="totp-setup-copy">
  <h2>扫描二维码</h2>
  <div class="setup-key-field">
    <div class="setup-key-heading">
      <h3 data-setup-key-label>设置密钥</h3>
      <ContextHelp
        data-action="explain-setup-key"
        label="说明设置密钥"
        text="无法扫码时，在验证器 App 中手工输入此设置密钥。"
      />
    </div>
    <code data-setup-key>{{ setup.setup_key }}</code>
  </div>
</div>
```

For the identity-verification state, keep only its heading before the form. For the configured state, replace the intro and standalone button with:

```vue
<div data-configured-authenticator-heading class="configured-authenticator-heading">
  <h2>验证器已绑定</h2>
  <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="reconfiguring = true">重新配置</button>
</div>
<div class="security-setting-row">
  <TotpProtectionLabel />
  <!-- existing switch -->
</div>
```

Remove the redundant paragraph from `TotpProtectionDialog.vue`. Add CSS:

```css
.configured-authenticator-heading, .setup-key-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.configured-authenticator-heading h2, .setup-key-heading h3 { margin: 0; }
.setup-key-field { display: grid; gap: var(--space-2); }
```

- [ ] **Step 4: Run detail and dialog tests and verify GREEN**

Run the command from Step 2.

Expected: both test files PASS with the API lifecycle unchanged.

- [ ] **Step 5: Commit Task 4**

```bash
git add packages/client-ui/src/views/TotpActivationView.vue \
  packages/client-ui/src/views/TotpActivationView.test.ts \
  packages/client-ui/src/components/settings/TotpProtectionDialog.vue \
  packages/client-ui/src/components/settings/TotpProtectionDialog.test.ts \
  packages/client-ui/src/styles/app.css
git commit -m "fix(ui): simplify authenticator guidance"
```

### Task 5: Update the real-browser contract and verify the complete UI slice

**Files:**
- Modify: `apps/clients/web/e2e/settings-auth.spec.ts`

- [ ] **Step 1: Update Playwright assertions for the approved layout**

After measuring equal theme widths, assert the option content is centered:

```ts
const alignments = await themeOptions.evaluateAll((elements) => elements.map((element) => getComputedStyle(element).justifyContent))
expect(new Set(alignments)).toEqual(new Set(['center']))
```

Replace QR paragraph spacing assertions with a heading-to-image check and assert the concise dialog has no paragraph:

```ts
const qrDialogSpacing = await qrDialog.evaluate((dialog) => {
  const header = dialog.querySelector('header')!.getBoundingClientRect()
  const image = dialog.querySelector('img')!.getBoundingClientRect()
  return { headingToImage: image.top - header.bottom }
})
expect(qrDialogSpacing.headingToImage).toBeGreaterThanOrEqual(12)
await expect(qrDialog.locator('p')).toHaveCount(0)
```

After focusing the protection help button, prove the tooltip stays inside the complete setting row:

```ts
const tooltipGeometry = await protectionLabel.locator('xpath=..').evaluate((row) => {
  const rowBox = row.getBoundingClientRect()
  const tooltipBox = row.querySelector<HTMLElement>('[role="tooltip"]')!.getBoundingClientRect()
  return { rowLeft: rowBox.left, rowRight: rowBox.right, tooltipLeft: tooltipBox.left, tooltipRight: tooltipBox.right }
})
expect(tooltipGeometry.tooltipLeft).toBeGreaterThanOrEqual(tooltipGeometry.rowLeft)
expect(tooltipGeometry.tooltipRight).toBeLessThanOrEqual(tooltipGeometry.rowRight)
```

Assert the configured heading owns the compact reconfigure operation and removed copy does not appear:

```ts
await expect(page.getByText('管理员 Token 只用于本次验证，不会保存在客户端。')).toHaveCount(0)
const configuredHeading = page.locator('[data-configured-authenticator-heading]')
await expect(configuredHeading.getByRole('button', { name: '重新配置' })).toBeVisible()
```

- [ ] **Step 2: Run unit tests, type checking, and Web build**

Run:

```bash
npm run test:run --workspace @termflow/client-ui
npm run typecheck --workspace @termflow/client-ui
npm run build:web
git diff --check
```

Expected: all commands exit `0`; Vitest reports no failures and Vite produces `apps/clients/web/dist`.

- [ ] **Step 3: Run the disposable browser fixture**

Run:

```bash
bash scripts/run-web-e2e.sh
```

Expected: desktop and mobile Playwright projects PASS, screenshots are written only under the reported temporary run directory, and the fixture removes its temporary process/database unless `TERMFLOW_E2E_KEEP=1` is explicitly set.

- [ ] **Step 4: Confirm the existing Compose instance was untouched**

Run:

```bash
docker compose --env-file /home/mcocdaa/AI_CODE/TermFlow/.env -f /home/mcocdaa/AI_CODE/TermFlow/deploy/compose.yaml ps
curl -fsS http://127.0.0.1:8765/healthz
```

Expected: the pre-existing `deploy-control-plane-1` remains healthy and returns `{"status":"ok"}`. Do not deploy the feature branch as part of browser testing.

- [ ] **Step 5: Commit Task 5**

```bash
git add apps/clients/web/e2e/settings-auth.spec.ts
git commit -m "test(ui): cover concise security settings"
```

- [ ] **Step 6: Review branch scope**

Run:

```bash
git status --short
git log --oneline main..HEAD
git diff --stat main...HEAD
```

Expected: clean status; only the design, plan, shared UI, CSS, unit tests, and settings-auth Playwright test appear in the branch.
