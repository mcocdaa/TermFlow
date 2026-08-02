# Mobile Terminal Menu and Keybar Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep mobile terminal menus and the modifier-key row inside the visual viewport, make Display/tmux highlighting reflect only real expansion, and completely remove the obsolete Pane-focus presentation path.

**Architecture:** Preserve `TerminalView.openMenu` as the controlled state for the two remaining titlebar menus. Put mobile popover geometry in responsive CSS, split the keybar into a clipping shell plus a horizontal scroller, and simplify the pointer viewport to generic scale/pan snapshots with no Pane-specific state. Control Plane, Node, tmux actions, terminal dimensions, and viewport-lock semantics do not change.

**Tech Stack:** Vue 3, TypeScript, Vitest, Vue Test Utils, CSS, Playwright, Docker Compose

---

## File map

- Delete `packages/client-ui/src/components/terminal/PaneFocusMenu.vue`: remove the obsolete UI entry point.
- Modify `packages/client-ui/src/views/TerminalView.vue`: retain only Display/tmux menu state and full-canvas orientation restore.
- Modify `packages/client-ui/src/components/terminal/TerminalCanvas.vue`: remove the Pane-focus presentation API while preserving generic viewport reset/snapshot/restore.
- Modify `packages/client-ui/src/composables/usePointerViewport.ts`: make snapshots `{ scale, panX, panY }` only.
- Modify `packages/client-ui/src/components/terminal/MobileKeyBar.vue`: separate background/clipping shell from the horizontal scroller.
- Modify `packages/client-ui/src/styles/app.css`: separate expanded fill from keyboard focus.
- Modify `packages/client-ui/src/styles/terminal-responsive.css`: constrain mobile menus and keybar scrolling.
- Modify focused Vitest files beside those components, plus `packages/client-ui/src/test/responsive-contract.test.ts`.
- Modify `apps/clients/web/e2e/control-center.spec.ts`: prove real mobile geometry and remove Pane-focus assumptions.

### Task 1: Remove Pane-focus presentation end to end

**Files:**
- Delete: `packages/client-ui/src/components/terminal/PaneFocusMenu.vue`
- Modify: `packages/client-ui/src/views/TerminalView.vue`
- Modify: `packages/client-ui/src/components/terminal/TerminalCanvas.vue`
- Modify: `packages/client-ui/src/composables/usePointerViewport.ts`
- Modify: `packages/client-ui/src/terminal/orientation.test.ts`
- Modify: `packages/client-ui/src/composables/usePointerViewport.test.ts`
- Modify: `packages/client-ui/src/components/terminal/TmuxControls.test.ts`
- Test: `packages/client-ui/src/views/TerminalView.test.ts`

- [ ] **Step 1: Write failing tests for the removed public behavior**

Replace the Pane-focus tests with assertions for a generic viewport and absent UI/API:

```ts
it('captures and restores only generic scale and pan', () => {
  const viewport = createPointerViewport({
    viewport: { width: 360, height: 800 },
    content: { width: 1200, height: 720 },
  })
  viewport.setTransform({ scale: 2, panX: -30, panY: -40 })
  const snapshot = viewport.snapshot()
  expect(snapshot).toEqual({ scale: 2, panX: -30, panY: -40 })
  expect(viewport).not.toHaveProperty('focusPane')
  viewport.reset()
  viewport.restore(snapshot)
  expect(viewport.snapshot()).toEqual(snapshot)
})
```

Use the same three-field value in `orientation.test.ts`:

```ts
state.portrait.viewport = { scale: 2, panX: -30, panY: -40 }
```

In `TerminalView.test.ts`, after topology loads:

```ts
expect(wrapper.find('[data-action="toggle-pane-focus-menu"]').exists()).toBe(false)
expect(wrapper.findComponent(TerminalCanvas).vm).not.toHaveProperty('focusPane')
```

Delete the PaneFocusMenu portion and import from `TmuxControls.test.ts`; the file continues to test only tmux controls.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/composables/usePointerViewport.test.ts \
  src/terminal/orientation.test.ts \
  src/views/TerminalView.test.ts \
  src/components/terminal/TmuxControls.test.ts
```

Expected: FAIL because snapshots still contain `focusedPaneId`, the viewport still exposes `focusPane`, and the titlebar still renders the focus trigger.

- [ ] **Step 3: Remove the production focus path**

In `usePointerViewport.ts`, define:

```ts
export interface PointerViewportSnapshot {
  scale: number
  panX: number
  panY: number
}
```

Remove `PaneGeometry`, `focusedPaneId`, all assignments that clear it, and `focusPane`. Return only:

```ts
return {
  state,
  pointerDown,
  pointerMove,
  pointerUp,
  setTransform,
  updateGeometry,
  reset,
  snapshot,
  restore,
}
```

In `TerminalCanvas.vue`, remove `PaneTopology`, `pendingFocusPane`, `focusPane`, `applyPaneFocus`, and `data-focused-pane`. Keep the exposed generic contract:

```ts
defineExpose({
  dimensions,
  bindings,
  lastActionResult,
  sendAction: session.sendAction,
  sendInput: session.sendInput,
  focus: session.focus,
  resetViewport,
  captureViewport: pointer.snapshot,
  restoreViewport,
})
```

In `TerminalView.vue`, delete `PaneFocusMenu`, remove `'pane'` from the menu union, and remove the Pane slot. Make restore generic:

```ts
function restoreOrientationView() {
  const saved = orientationViews[orientation.value].viewport
  if (saved) terminalCanvas.value?.restoreViewport(saved)
  else terminalCanvas.value?.resetViewport()
}
```

Delete `PaneFocusMenu.vue` after all imports and consumers are gone.

- [ ] **Step 4: Verify focus removal is GREEN and no production reference remains**

Run the focused test command from Step 2, then:

```bash
rg -n "PaneFocusMenu|toggle-pane-focus-menu|focusedPaneId|focusPane|applyPaneFocus|pendingFocusPane|data-focused-pane" \
  packages/client-ui/src \
  --glob '!**/*.test.ts'
```

Expected: tests PASS and `rg` exits 1 with no production matches.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src
git commit -m "refactor(client): remove pane focus presentation"
```

### Task 2: Bind menu fill and mobile geometry to real expansion

**Files:**
- Modify: `packages/client-ui/src/components/terminal/DisplayMenu.test.ts`
- Modify: `packages/client-ui/src/components/terminal/TmuxControls.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/styles/terminal-responsive.css`
- Test: `packages/client-ui/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing menu-state and CSS contract tests**

In both component tests, close an opened menu through its trigger and assert controlled state returns to false after the parent updates the prop:

```ts
await trigger.trigger('click')
await wrapper.setProps({ open: true })
expect(trigger.attributes('aria-expanded')).toBe('true')
await trigger.trigger('click')
expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
await wrapper.setProps({ open: false })
expect(trigger.attributes('aria-expanded')).toBe('false')
expect(wrapper.find('[role="menu"]').exists()).toBe(false)
```

Extend `responsive-contract.test.ts`:

```ts
expect(appCss).toContain(".titlebar-button[aria-expanded='true'] {")
expect(appCss).toContain('@media (hover: hover) and (pointer: fine) {')
expect(appCss).not.toContain('.titlebar-button:focus-visible')
expect(css).toContain('.titlebar-menu { position: static;')
expect(css).toContain('.terminal-titlebar .floating-menu {')
expect(css).toContain('max-height: calc(100dvh - 3.25rem - 2 * var(--space-2));')
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/components/terminal/DisplayMenu.test.ts \
  src/components/terminal/TmuxControls.test.ts \
  src/test/responsive-contract.test.ts
```

Expected: component state assertions pass, but the CSS contract fails because focus still shares filled styling and mobile panels still use desktop anchoring.

- [ ] **Step 3: Implement the minimal styling change**

In `app.css`, change the active fill selector to:

```css
.titlebar-button[aria-expanded='true'] {
  border-color: var(--color-accent);
  background: color-mix(in srgb, var(--color-accent) 13%, var(--color-elevated));
  color: var(--color-accent);
}
@media (hover: hover) and (pointer: fine) {
  .titlebar-button:hover:not(:disabled) {
    border-color: var(--color-accent);
    background: color-mix(in srgb, var(--color-accent) 13%, var(--color-elevated));
    color: var(--color-accent);
  }
}
```

Do not add a local focus fill rule; `reset.css` already supplies the accessible `:focus-visible` outline. Restrict hover fill to fine pointers so touch browsers cannot retain a sticky `:hover` after closing.

Inside the mobile/coarse-pointer block in `terminal-responsive.css`, add:

```css
.titlebar-menu { position: static; height: 2.35rem; }
.terminal-titlebar .floating-menu {
  inset-block-start: calc(100% + var(--space-1));
  inset-inline: max(var(--space-2), env(safe-area-inset-left)) max(var(--space-2), env(safe-area-inset-right));
  width: auto;
  min-width: 0;
  max-height: calc(100dvh - 3.25rem - 2 * var(--space-2));
  overflow-y: auto;
  overscroll-behavior: contain;
}
.terminal-titlebar .action-menu { width: auto; }
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: all selected Vitest files PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/components/terminal/DisplayMenu.test.ts \
  packages/client-ui/src/components/terminal/TmuxControls.test.ts \
  packages/client-ui/src/styles/app.css \
  packages/client-ui/src/styles/terminal-responsive.css \
  packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(client): constrain mobile terminal menus"
```

### Task 3: Contain the mobile modifier-key scroller

**Files:**
- Modify: `packages/client-ui/src/components/terminal/MobileKeyBar.vue`
- Modify: `packages/client-ui/src/components/terminal/MobileKeyBar.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/styles/terminal-responsive.css`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing component and CSS contract tests**

In `MobileKeyBar.test.ts`, assert the separate ownership layers:

```ts
it('separates viewport coverage from horizontal key scrolling', () => {
  const wrapper = mountKeyBar({
    prefix: 'C-a',
    controller: new MobileModifierController(),
  })
  const shell = wrapper.get('.mobile-keybar-shell')
  const scroller = shell.get('.mobile-keybar')
  expect(shell.attributes('aria-hidden')).toBeUndefined()
  expect(scroller.attributes('aria-label')).toBe('移动端修饰键')
  expect(scroller.findAll('button')).toHaveLength(6)
})
```

Update the responsive contract:

```ts
expect(css).toContain('.mobile-keybar-shell {')
expect(css).toContain('grid-row: 3;')
expect(css).toContain('overflow: hidden;')
expect(css).toContain('.mobile-keybar {')
expect(css).toContain('overscroll-behavior-x: none;')
expect(css).toContain('overscroll-behavior-y: none;')
expect(css).not.toContain('overscroll-behavior-inline: contain;')
expect(css).not.toContain('overscroll-behavior-block: none;')
expect(appCss).toContain('.mobile-keybar-shell, .mobile-keybar { display: none; }')
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/components/terminal/MobileKeyBar.test.ts \
  src/test/responsive-contract.test.ts
```

Expected: FAIL because `.mobile-keybar-shell` does not exist and the current row uses `overscroll-behavior-inline: contain`.

- [ ] **Step 3: Add the shell and constrained scroller**

Replace the template in `MobileKeyBar.vue` with the same six controls inside two ownership layers:

```vue
<template>
  <div class="mobile-keybar-shell">
    <div class="mobile-keybar" aria-label="移动端修饰键" @pointermove.stop>
      <button v-for="key in modifierKeys" :key="key.id" type="button" :disabled="disabled" :aria-pressed="controller.state[key.id] !== 'off'" @click="controller.press(key.id)">{{ key.label }}<span v-if="controller.state[key.id] === 'sticky'" class="locked-indicator" aria-label="已锁定" /></button>
      <button type="button" :disabled="disabled" @click="special('Escape')">Esc</button>
      <button type="button" :disabled="disabled" @click="special('Tab')">Tab</button>
      <button type="button" :disabled="disabled || !usablePrefix" :aria-pressed="controller.state.prefix" :title="usablePrefix ? `实际 Prefix：${prefix}` : 'Prefix 未报告'" @click="sendPrefix">Prefix</button>
    </div>
  </div>
</template>
```

In `app.css`, hide both layers outside the mobile breakpoint:

```css
.mobile-keybar-shell, .mobile-keybar { display: none; }
```

Replace the mobile keybar rules in `terminal-responsive.css` with:

```css
.mobile-keybar-shell {
  position: static;
  grid-row: 3;
  z-index: 40;
  min-width: 0;
  width: 100%;
  max-width: 100%;
  display: block;
  overflow: hidden;
  padding: var(--space-2) max(var(--space-2), env(safe-area-inset-right)) var(--space-2) max(var(--space-2), env(safe-area-inset-left));
  padding-block-end: max(var(--space-2), env(safe-area-inset-bottom));
  border-block-start: 1px solid var(--color-border);
  background: var(--color-panel);
}
.mobile-keybar {
  min-width: 0;
  width: 100%;
  max-width: 100%;
  display: flex;
  gap: var(--space-1);
  overflow-x: auto;
  overflow-y: hidden;
  touch-action: pan-x;
  overscroll-behavior-x: none;
  overscroll-behavior-y: none;
}
```

Keep the existing button rules unchanged.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: both files PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/components/terminal/MobileKeyBar.vue \
  packages/client-ui/src/components/terminal/MobileKeyBar.test.ts \
  packages/client-ui/src/styles/app.css \
  packages/client-ui/src/styles/terminal-responsive.css \
  packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(client): contain mobile terminal keybar"
```

### Task 4: Prove the behavior in a real mobile browser

**Files:**
- Modify: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] **Step 1: Write failing real-browser assertions**

Remove every Pane-focus locator and action. For mobile projects assert absence:

```ts
await expect(page.getByRole('button', { name: '聚焦 Pane' })).toHaveCount(0)
```

Import `Locator` with `Page`, then after opening each menu assert its rectangle remains inside the visual viewport:

```ts
import { expect, test, type Locator, type Page } from '@playwright/test'

async function expectInsideVisualViewport(locator: Locator, page: Page) {
  const box = await locator.boundingBox()
  expect(box).not.toBeNull()
  const viewport = await page.evaluate(() => ({
    left: window.visualViewport?.offsetLeft ?? 0,
    top: window.visualViewport?.offsetTop ?? 0,
    width: window.visualViewport?.width ?? window.innerWidth,
    height: window.visualViewport?.height ?? window.innerHeight,
  }))
  expect(box!.x).toBeGreaterThanOrEqual(viewport.left - 1)
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.left + viewport.width + 1)
  expect(box!.y).toBeGreaterThanOrEqual(viewport.top - 1)
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.top + viewport.height + 1)
}
```

For both Display and tmux on mobile, capture the closed visual state, open and prove the active fill changes, then close and prove the exact closed state is restored:

```ts
const buttonStyle = (trigger: Locator) => trigger.evaluate((element) => {
  const style = getComputedStyle(element)
  return { backgroundColor: style.backgroundColor, color: style.color, borderColor: style.borderColor }
})
const closedStyle = await buttonStyle(trigger)
await trigger.click()
await expect(trigger).toHaveAttribute('aria-expanded', 'true')
await expectInsideVisualViewport(menu, page)
expect(await buttonStyle(trigger)).not.toEqual(closedStyle)
await trigger.click()
await expect(trigger).toHaveAttribute('aria-expanded', 'false')
await expect(menu).toBeHidden()
expect(await buttonStyle(trigger)).toEqual(closedStyle)
```

For keybar containment, measure `.mobile-keybar-shell` separately from `.mobile-keybar`. Assert the shell inline edges equal the viewport edges within one pixel, its bottom covers the visual viewport bottom, and the inner scroller is contained. Dispatch horizontal drags from start to end and end to start, including a second drag beyond each boundary; after each sequence assert root rectangles, scroll positions, visual viewport offsets, and shell geometry are unchanged.

- [ ] **Step 2: Run isolated mobile E2E acceptance after the unit-level fixes**

Run:

```bash
TERMFLOW_E2E_PROJECTS='mobile-portrait,mobile-landscape' ./scripts/run-web-e2e.sh
```

Expected: mobile portrait and mobile landscape PASS. Tasks 1-3 already established RED/GREEN at the unit and CSS-contract boundaries; this task adds real-browser acceptance without additional production code.

- [ ] **Step 3: Update the existing scenario without changing terminal semantics**

Delete the focus-menu sequence that selected `显示完整终端`; after selecting `100% 实际字号`, use the initial/reset full-canvas transform directly. Keep the existing assertions for horizontal pan, vertical pan, pinch, lock preservation, real tmux mouse targeting, long-press selection, and desktop wheel reporting.

Retain Display/tmux mutual exclusion with only two menus:

```ts
await displayTrigger.click()
await tmuxTrigger.click()
await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeHidden()
await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeVisible()
await displayTrigger.click()
await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeVisible()
```

- [ ] **Step 4: Run all three isolated browser projects**

Run:

```bash
./scripts/run-web-e2e.sh
```

Expected: desktop, mobile portrait, and mobile landscape all PASS; the script removes its disposable environment after completion.

- [ ] **Step 5: Commit**

```bash
git add apps/clients/web/e2e/control-center.spec.ts
git commit -m "test(web): cover mobile menu and keybar containment"
```

### Task 5: Full verification, Docker deployment, and cleanup

**Files:**
- Verify only; no planned production file changes.

- [ ] **Step 1: Run shared-client verification**

```bash
npm run test:run --workspace @termflow/client-ui
npm run typecheck --workspace @termflow/client-ui
npm run build:web
```

Expected: all tests PASS, Vue typecheck PASS, and the Web C production bundle builds.

- [ ] **Step 2: Run repository verification**

Use a disposable config-only admin token; do not deploy it:

```bash
TERMFLOW_ADMIN_TOKEN='verify-only-admin-token-that-is-not-deployed' ./scripts/verify.sh
```

Expected: frontend suites, Python suites, contracts, typecheck, build, Ruff, mypy, and Compose config all PASS.

- [ ] **Step 3: Build and force-recreate only the Control Plane image**

```bash
docker compose -f deploy/compose.yaml build control-plane
docker compose -f deploy/compose.yaml up -d --no-deps --force-recreate control-plane
```

Do not remove or recreate `termflow-data`, and do not delete or modify a real Term/tmux session.

- [ ] **Step 4: Verify deployment identity and health**

```bash
docker compose -f deploy/compose.yaml ps control-plane
docker inspect deploy-control-plane-1 --format '{{.Image}} {{.State.Health.Status}} {{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}'
curl --fail --silent http://127.0.0.1:8765/healthz
```

Expected: the container is healthy, uses the newly built image, retains `termflow-data:/data`, and `/healthz` returns `{"status":"ok"}`.

- [ ] **Step 5: Run deployed read-only UI smoke**

```bash
TERMFLOW_E2E_DEPLOYED=1 npx playwright test \
  --config apps/clients/web/playwright.config.ts \
  apps/clients/web/e2e/deployed-smoke.spec.ts
```

Expected: desktop and mobile deployed smoke projects PASS without destructive API calls or terminal input.

- [ ] **Step 6: Merge and clean the isolated worktree**

From the main checkout, fast-forward the verified feature branch, then remove only the worktree created for this plan and delete its feature branch. Preserve all unrelated worktrees and user changes. Confirm:

```bash
git status --short --branch
git worktree list
```

Expected: `main` contains the implementation, the task worktree is gone, unrelated worktrees remain, and `main` is clean.
