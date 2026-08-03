# Settings and TOTP Wizard Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a form-like relay URL field, a root Compose environment example for the public reverse-proxy URL, and a balanced three-step TOTP setup wizard with explicit progress states.

**Architecture:** Keep all authentication APIs and state transitions unchanged. Derive presentation state inside `TotpActivationView`, expose it through semantic DOM attributes for accessibility and tests, and use the existing shared CSS/token system for the new centered desktop layout and stacked mobile layout. Make root `.env.example` the single tracked Compose example so the public URL configuration cannot drift across two files.

**Tech Stack:** Vue 3, TypeScript, Vitest, Vue Test Utils, Playwright, CSS custom properties, pytest, Docker Compose.

---

## File map

- `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`: server URL field structure and QR trigger.
- `packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts`: field hierarchy, copy, and QR behavior.
- `packages/client-ui/src/views/TotpActivationView.vue`: wizard state derivation, card headers, binding layout, setup-key disclosure, and copy action.
- `packages/client-ui/src/views/TotpActivationView.test.ts`: TOTP state transitions and accessibility contracts.
- `packages/client-ui/src/styles/app.css`: server field rhythm, three-state stepper, centered binding group, and responsive layout.
- `packages/client-ui/src/test/responsive-contract.test.ts`: CSS-level mobile layout contract.
- `apps/clients/web/e2e/settings-auth.spec.ts`: authenticated browser geometry and complete lifecycle coverage.
- `.env.example`: canonical root Compose environment example.
- `deploy/env.example`: removed after the root example replaces it.
- `README.md`, `docs/operations.md`: root environment example and reverse-proxy URL instructions.
- `tests/docs/test_documentation_contract.py`: tracked environment-example contract.

### Task 1: Make the root environment example authoritative

**Files:**

- Create: `.env.example`
- Delete: `deploy/env.example`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Write the failing documentation contract**

Change the environment-example lookup and add root-path assertions:

```python
def test_operations_docs_define_external_edge_secrets_and_native_toolchains() -> None:
    operations = Path("docs/operations.md").read_text()
    readme = Path("README.md").read_text()
    clients = Path("apps/clients/README.md").read_text()
    env_example = Path(".env.example").read_text()

    assert "[.env.example](.env.example)" in readme
    assert "deploy/env.example" not in readme
    assert "TERMFLOW_PUBLIC_BASE_URL" in env_example
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" in env_example
    assert "反向代理" in env_example
```

Keep the existing master-key and platform assertions below these checks.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run --frozen pytest tests/docs/test_documentation_contract.py::test_operations_docs_define_external_edge_secrets_and_native_toolchains -q
```

Expected: FAIL because root `.env.example` does not exist.

- [ ] **Step 3: Move the tracked example to the root and update documentation**

Create `.env.example` with the existing safe Compose variables. Keep loopback defaults runnable and document the production replacement directly above the public origin values:

```dotenv
# Browsers and native clients must use this externally reachable origin.
# Behind a reverse proxy, replace both loopback values with its HTTPS origin.
TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:8765
TERMFLOW_TRUSTED_WEB_ORIGINS=http://127.0.0.1:8765
```

Preserve the existing generated-token instruction, host port, session limits, enrollment limits, terminal limits, and TOTP-key warning from `deploy/env.example`. Remove `deploy/env.example`. Change README and operations documentation to tell operators to copy `.env.example` to the ignored `.env` before running Compose.

- [ ] **Step 4: Run the focused test and Compose contract**

Run:

```bash
uv run --frozen pytest tests/docs/test_documentation_contract.py tests/deploy/test_compose_contract.py -q
docker compose --env-file .env.example -f deploy/compose.yaml config --quiet
```

Expected: documentation and Compose contracts pass; Compose renders without a missing variable error.

- [ ] **Step 5: Commit**

```bash
git add .env.example deploy/env.example README.md docs/operations.md tests/docs/test_documentation_contract.py
git commit -m "docs: expose public server URL environment"
```

### Task 2: Render the service URL as a normal field

**Files:**

- Modify: `packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts`
- Modify: `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write the failing field-hierarchy test**

Replace the `h3` expectation with the standard field-label contract:

```ts
const field = wrapper.get('[data-server-field]')
const label = field.get('[data-server-label]')
expect(label.element.tagName).toBe('SPAN')
expect(label.text()).toContain('服务网址')
expect(label.attributes('id')).toBe('server-url-label')
expect(field.find('h3').exists()).toBe(false)
expect(field.get('#server-url-value').text()).toBe('https://relay.example.com')
```

- [ ] **Step 2: Run the component test and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/components/settings/ServerConnectionPanel.test.ts
```

Expected: FAIL because `data-server-label` is currently a `div` containing an `h3`.

- [ ] **Step 3: Implement the field structure and spacing**

Use a label-like field header with the QR action beside it:

```vue
<div class="server-field-heading">
  <span id="server-url-label" data-server-label class="field-label">服务网址</span>
  <button ... aria-label="显示服务网址二维码">
    <QrCode ... />
  </button>
</div>
<div class="server-address-row" aria-labelledby="server-url-label">
  <code id="server-url-value" data-server-issuer>{{ issuer }}</code>
  <button ...>复制</button>
</div>
```

Remove the `2.75rem` minimum height from `.server-field-heading`, align the label and QR trigger on the text baseline, and keep `.server-field` at `var(--space-2)` so the URL box follows its label with the same rhythm as the computer-name field.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/components/settings/ServerConnectionPanel.test.ts
```

Expected: one passing test with copy and QR behavior unchanged.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/components/settings/ServerConnectionPanel.vue packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts packages/client-ui/src/styles/app.css
git commit -m "fix(ui): align relay URL field hierarchy"
```

### Task 3: Implement the three-state TOTP wizard

**Files:**

- Modify: `packages/client-ui/src/views/TotpActivationView.test.ts`
- Modify: `packages/client-ui/src/views/TotpActivationView.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing progress-state assertions**

Update the lifecycle test to expect three steps and state transitions:

```ts
const steps = () => wrapper.findAll('[data-guide-step]')
expect(steps()).toHaveLength(3)
expect(steps().map((step) => step.attributes('data-state'))).toEqual([
  'current', 'upcoming', 'upcoming',
])
expect(steps()[0]?.attributes('aria-current')).toBe('step')

// After beginTotpSetup resolves:
expect(steps().map((step) => step.attributes('data-state'))).toEqual([
  'complete', 'current', 'upcoming',
])
expect(wrapper.get('[data-wizard-card-title]').text()).toBe('绑定验证器')
expect(wrapper.get('[data-wizard-progress]').text()).toBe('第 2 步，共 3 步')

// After confirmTotpSetup resolves:
expect(steps().map((step) => step.attributes('data-state'))).toEqual([
  'complete', 'complete', 'current',
])
```

After protection is enabled, assert all three stages are `complete` and no item has `aria-current`.

- [ ] **Step 2: Write failing binding-layout and setup-key tests**

Before changing the component, add:

```ts
expect(wrapper.get('[data-totp-bind-layout]').exists()).toBe(true)
const disclosure = wrapper.get('[data-action="toggle-setup-key"]')
expect(disclosure.attributes('aria-expanded')).toBe('false')
expect(wrapper.find('[data-setup-key]').exists()).toBe(false)
await disclosure.trigger('click')
expect(disclosure.attributes('aria-expanded')).toBe('true')
expect(wrapper.get('[data-setup-key]').text()).toBe('SETUPKEY')
await wrapper.get('[data-action="copy-setup-key"]').trigger('click')
expect(writeText).toHaveBeenCalledWith('SETUPKEY')
```

Provide a fake runtime clipboard spy in the test fixture.

- [ ] **Step 3: Run the view test and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/TotpActivationView.test.ts
```

Expected: FAIL because five undifferentiated stages and the exposed key layout still exist.

- [ ] **Step 4: Implement deterministic wizard presentation state**

Replace `guideSteps` with:

```ts
const guideSteps = ['验证身份', '绑定验证器', '启用登录保护'] as const
const currentStep = computed<1 | 2 | 3>(() => {
  if (setup.value) return 2
  if (status.configured && !reconfiguring.value) return 3
  return 1
})
const wizardComplete = computed(() => status.enabled && !reconfiguring.value && setup.value === null)
function guideStepState(index: number) {
  if (wizardComplete.value || index < currentStep.value) return 'complete'
  return index === currentStep.value ? 'current' : 'upcoming'
}
```

Render an explicit marker containing either the stage number or `✓`, set `data-state`, and set `aria-current="step"` only for the current stage.

- [ ] **Step 5: Implement the card header and centered binding task**

Add a reusable card header inside the existing view:

```vue
<header class="totp-wizard-card-heading">
  <h2 data-wizard-card-title>{{ wizardTitle }}</h2>
  <span data-wizard-progress>第 {{ currentStep }} 步，共 3 步</span>
</header>
```

For a pending setup, render `.totp-bind-layout` with a QR column and a verification form column. Replace the always-visible key with a button labeled `无法扫描？使用设置密钥`, controlled by `setupKeyExpanded`. On expansion, show the key in a bordered code row and a copy button. Reset expansion and copied state whenever a new setup starts or confirmation completes.

- [ ] **Step 6: Implement balanced desktop and mobile CSS**

Use a centered desktop group and responsive stack:

```css
.totp-guide-steps { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.totp-bind-layout {
  width: min(100%, 46rem);
  margin-inline: auto;
  display: grid;
  grid-template-columns: minmax(12rem, 16rem) minmax(18rem, 1fr);
  align-items: center;
  gap: clamp(var(--space-5), 5vw, 4rem);
}
```

Give current, complete, and upcoming markers distinct token-based border/background/text colors. At `max-width: 47.99rem`, keep the three stages horizontal and switch `.totp-bind-layout` to one column with the QR above the form.

- [ ] **Step 7: Run the view test and shared UI suite**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/TotpActivationView.test.ts
npm run test:run --workspace @termflow/client-ui
```

Expected: focused lifecycle tests and the complete shared UI suite pass.

- [ ] **Step 8: Commit**

```bash
git add packages/client-ui/src/views/TotpActivationView.vue packages/client-ui/src/views/TotpActivationView.test.ts packages/client-ui/src/styles/app.css
git commit -m "fix(ui): redesign TOTP activation wizard"
```

### Task 4: Lock the layout with responsive and authenticated browser evidence

**Files:**

- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/e2e/settings-auth.spec.ts`

- [ ] **Step 1: Write failing responsive CSS assertions**

Add source-contract assertions that the mobile rule keeps the guide at three columns and stacks only the binding body:

```ts
expect(css).toContain('.totp-guide-steps { grid-template-columns: repeat(3, minmax(0, 1fr)); }')
expect(css).toMatch(/@media \(max-width: 47\.99rem\)[\s\S]*\.totp-bind-layout[^}]*grid-template-columns: 1fr/)
expect(css).not.toMatch(/@media \(max-width: 47\.99rem\)[\s\S]*\.totp-guide-steps[^}]*grid-template-columns: 1fr/)
```

- [ ] **Step 2: Update the browser lifecycle expectations before implementation**

Change the server field lookup from a heading to `[data-server-label]`. Expect three steps, verify step 1 is current initially, and after beginning setup verify step 2 is current. Open the setup-key disclosure before reading the secret for TOTP generation.

Add desktop geometry evidence:

```ts
const bindingGeometry = await page.evaluate(() => {
  const card = document.querySelector<HTMLElement>('.totp-guide-card')!.getBoundingClientRect()
  const layout = document.querySelector<HTMLElement>('[data-totp-bind-layout]')!.getBoundingClientRect()
  const qr = document.querySelector<HTMLElement>('.themed-qr-code')!.getBoundingClientRect()
  return {
    centerDelta: Math.abs((layout.left + layout.width / 2) - (card.left + card.width / 2)),
    qrInset: qr.left - card.left,
  }
})
expect(bindingGeometry.centerDelta).toBeLessThan(2)
expect(bindingGeometry.qrInset).toBeGreaterThan(96)
```

- [ ] **Step 3: Run focused tests and verify expected failures**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
```

Expected: FAIL until the new responsive selectors are present.

- [ ] **Step 4: Finish selectors and run isolated browser coverage**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
scripts/run-web-e2e.sh apps/clients/web/e2e/settings-auth.spec.ts --project=desktop
```

Expected: responsive contract passes; isolated desktop flow captures the themed QR, centered setup group, correct step transitions, login-protection flow, and no console errors.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/test/responsive-contract.test.ts apps/clients/web/e2e/settings-auth.spec.ts
git commit -m "test(ui): cover balanced TOTP wizard layout"
```

### Task 5: Verify the deliverable and update the running Web C

**Files:**

- Modify locally after merge: ignored `.env`
- No tracked source changes expected.

- [ ] **Step 1: Run the complete client verification**

Run:

```bash
npm run test:run
npm run typecheck
npm run build:web
uv run --frozen pytest tests/docs/test_documentation_contract.py tests/deploy/test_compose_contract.py -q
git diff --check
```

Expected: all workspace tests, all workspace typechecks, the Web C production build, documentation/deploy tests, and whitespace checks pass.

- [ ] **Step 2: Inspect browser screenshots**

Inspect the isolated browser screenshot for the binding step at 1440×900. Confirm the task title is left-aligned, the content group is centered, the QR is not attached to the card edge, and the setup-key row is closed before interaction.

- [ ] **Step 3: Complete branch integration using the finishing workflow**

Use `superpowers:requesting-code-review`, `superpowers:verification-before-completion`, and `superpowers:finishing-a-development-branch`. Merge only after review and fresh verification.

- [ ] **Step 4: Update the ignored local Compose environment**

Preserve the existing administrator token and TOTP key. Add only:

```dotenv
TERMFLOW_PUBLIC_BASE_URL=http://127.0.0.1:8765
TERMFLOW_TRUSTED_WEB_ORIGINS=http://127.0.0.1:8765
```

For a production reverse proxy, the deployer replaces both values with the public HTTPS origin.

- [ ] **Step 5: Rebuild or safely overlay and recreate the local container**

First run the official image build and verification:

```bash
scripts/build-control-plane-image.sh deploy-control-plane
scripts/verify-control-plane-image.sh deploy-control-plane
docker compose --env-file .env -f deploy/compose.yaml up -d --no-build --force-recreate control-plane
```

If registry DNS again prevents pulling unchanged base images, derive a local static-resource overlay from the currently verified backend image, verify it with `scripts/verify-control-plane-image.sh`, and recreate only `control-plane`. Never remove `termflow-data`.

- [ ] **Step 6: Verify the live deployment**

Run:

```bash
docker compose --env-file .env -f deploy/compose.yaml ps
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/ | sha256sum
sha256sum apps/clients/web/dist/index.html
```

Expected: container is healthy, health response is `{"status":"ok"}`, served and local `index.html` hashes match, and `termflow-data:/app/data` remains mounted.
