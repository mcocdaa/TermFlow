# Mobile Terminal Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Web C a compact mobile header, explicit local-versus-remote touch control, reliable xterm/tmux mouse gestures, and a bottom key row that never exposes the terminal underlay.

**Architecture:** Keep `TerminalView` as the owner of menu and lock state, put gesture arbitration in a new pure composable, and let `TerminalAdapter` translate the selected gesture into standard xterm mouse events. Reuse the native xterm visual scaling already merged in `4033ede`; do not reintroduce CSS scaling or encode tmux mouse protocol in Web C.

**Tech Stack:** Vue 3.5, TypeScript, xterm.js 6, Lucide Vue, Vitest, Vue Test Utils, Playwright, the repository's disposable real-tmux Web fixture.

---

## Baseline and File Map

Native xterm visual scaling and scaled mouse fidelity are already merged on `main` at `4033ede`, with the fit-geometry stabilization committed at `ef4168c`. Do not repeat that work. The main worktree was clean apart from this plan when the file map below was finalized.

Execute this plan in an isolated worktree created from `ef4168c` or a later `main`. Before integration, merge the latest `main` and preserve all subsequent terminal geometry fixes.

The remaining implementation changes exactly **22 files: 20 modified and 2 created**.

**Create:**

- `apps/clients/web/src/composables/useTerminalTouchGestures.ts`: pure touch-mode state machine.
- `apps/clients/web/src/composables/useTerminalTouchGestures.test.ts`: fake-timer gesture contract tests.

**Modify:**

- `apps/clients/web/src/App.vue`: responsive logout icon.
- `apps/clients/web/src/App.test.ts`: logout icon/accessibility regression.
- `apps/clients/web/src/views/TerminalView.vue`: lock state and canvas wiring.
- `apps/clients/web/src/views/TerminalView.test.ts`: mobile titlebar and orientation state.
- `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`: lock toggle and compact metadata.
- `apps/clients/web/src/components/terminal/DisplayMenu.vue`: mobile-hideable label.
- `apps/clients/web/src/components/terminal/DisplayMenu.test.ts`: accessible icon-trigger contract.
- `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`: remove duplicate mobile drawer.
- `apps/clients/web/src/components/terminal/PaneFocusMenu.vue`: mobile-hideable label.
- `apps/clients/web/src/components/terminal/TmuxControls.test.ts`: one tmux menu on every viewport.
- `apps/clients/web/src/components/terminal/TerminalCanvas.vue`: gesture arbiter integration.
- `apps/clients/web/src/components/terminal/TerminalCanvas.test.ts`: locked/unlocked event routing.
- `apps/clients/web/src/terminal/terminalAdapter.ts`: synthetic mouse dispatch boundary.
- `apps/clients/web/src/terminal/terminalAdapter.test.ts`: force-selection modifier contract.
- `apps/clients/web/src/composables/useTerminalSession.ts`: expose adapter mouse dispatch.
- `apps/clients/web/src/styles/app.css`: shared control styles and drawer removal.
- `apps/clients/web/src/styles/terminal-responsive.css`: compact titlebar and three-row mobile layout.
- `apps/clients/web/src/test/responsive-contract.test.ts`: CSS/layout regression contract.
- `apps/clients/web/e2e/control-center.spec.ts`: real mobile touch and layout acceptance.
- `docs/web-client.md`: current mobile interaction documentation.

### Task 1: Make logout icon-only on phones

**Files:**

- Modify: `apps/clients/web/src/App.test.ts`
- Modify: `apps/clients/web/src/App.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write the failing header test**

Import `afterEach` from Vitest and `sessionState` from the session store. Reset the global state after every test:

```ts
afterEach(() => {
  sessionState.authenticated = false
  sessionState.expiresAt = null
})
```

Authenticate explicitly and add this assertion to the shell test:

```ts
sessionState.authenticated = true
const dashboard = await renderAt('/')
const logout = dashboard.get('[data-action="logout"]')
expect(logout.attributes('aria-label')).toBe('退出登录')
expect(logout.attributes('title')).toBe('退出登录')
expect(logout.find('svg').exists()).toBe(true)
expect(logout.get('.logout-label').text()).toBe('退出')
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/App.test.ts
```

Expected: FAIL because the current logout button has no data action, SVG, accessible name, or `.logout-label`.

- [ ] **Step 3: Implement the responsive logout control**

Change the button in `App.vue` and import `LogOut` from `@lucide/vue`:

```vue
<button
  v-if="sessionState.authenticated"
  data-action="logout"
  class="text-button logout-button"
  type="button"
  aria-label="退出登录"
  title="退出登录"
  @click="logout"
>
  <LogOut :size="18" aria-hidden="true" />
  <span class="logout-label">退出</span>
</button>
```

Add shared alignment in `app.css`:

```css
.logout-button { display: inline-flex; align-items: center; justify-content: center; gap: var(--space-2); }
```

Add a dedicated phone/coarse-pointer media rule so landscape phones also use the icon:

```css
@media (max-width: 47.99rem), (pointer: coarse) {
  .logout-button { width: 2.75rem; padding: 0; }
  .logout-label { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
}
```

- [ ] **Step 4: Run the test and typecheck**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/App.test.ts
npm run typecheck
```

Expected: App tests pass and TypeScript reports no errors.

- [ ] **Step 5: Commit the responsive logout**

```bash
git add apps/clients/web/src/App.vue apps/clients/web/src/App.test.ts apps/clients/web/src/styles/app.css
git commit -m "feat(web): compact mobile logout control"
```

### Task 2: Consolidate the mobile titlebar into four icon controls

**Files:**

- Modify: `apps/clients/web/src/views/TerminalView.test.ts`
- Modify: `apps/clients/web/src/views/TerminalView.vue`
- Modify: `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/PaneFocusMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/TmuxControls.test.ts`
- Modify: `apps/clients/web/src/styles/app.css`
- Modify: `apps/clients/web/src/styles/terminal-responsive.css`
- Modify: `apps/clients/web/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing titlebar assertions**

Extend the 360×800 `TerminalView.test.ts` case:

```ts
for (const action of [
  'toggle-display-menu',
  'toggle-pane-focus-menu',
  'toggle-tmux-menu',
  'toggle-touch-lock',
]) {
  expect(wrapper.get(`[data-action="${action}"]`).find('svg').exists()).toBe(true)
}
const lock = wrapper.get('[data-action="toggle-touch-lock"]')
expect(lock.attributes('aria-label')).toBe('锁定画布')
expect(lock.attributes('aria-pressed')).toBe('false')
await lock.trigger('click')
expect(lock.attributes('aria-pressed')).toBe('true')

Object.defineProperties(window, {
  innerWidth: { value: 800, configurable: true },
  innerHeight: { value: 360, configurable: true },
})
window.dispatchEvent(new Event('resize'))
await flushPromises()
expect(wrapper.get('[data-action="toggle-touch-lock"]').attributes('aria-pressed')).toBe('true')
expect(wrapper.find('[data-action="toggle-mobile-drawer"]').exists()).toBe(false)
expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
```

At the end of the test, unmount and mount `App` again at the same Term route, then assert the fresh titlebar resets safely:

```ts
wrapper.unmount()
const remounted = mount(App, { attachTo: document.body, global: { plugins: [router] } })
await flushPromises()
expect(remounted.get('[data-action="toggle-touch-lock"]').attributes('aria-pressed')).toBe('false')
remounted.unmount()
```

In `DisplayMenu.test.ts`, assert the trigger keeps the accessible label while its visible copy has a CSS hook:

```ts
const trigger = wrapper.get('[data-action="toggle-display-menu"]')
expect(trigger.attributes('aria-label')).toBe('显示设置')
expect(trigger.get('.control-label').text()).toBe('显示')
```

Replace the drawer-specific tests in `TmuxControls.test.ts` with:

```ts
expect(wrapper.find('[data-action="toggle-mobile-drawer"]').exists()).toBe(false)
expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
const trigger = wrapper.get('[data-action="toggle-tmux-menu"]')
expect(trigger.attributes('aria-label')).toBe('tmux 操作')
expect(trigger.get('.control-label').text()).toBe('tmux 操作')
```

Update `responsive-contract.test.ts` to require the mobile CSS hooks and reject the removed drawer selectors:

```ts
expect(css).toContain('.control-label, .menu-chevron, [data-computer-name] { display: none; }')
expect(css).toContain('.terminal-view { height: 100dvh; grid-template-rows: auto minmax(0, 1fr) auto; }')
expect(css).not.toContain('.mobile-action-trigger')
expect(css).not.toContain('.mobile-action-drawer')
```

After the test's existing `const appCss = readFileSync(...)` line, add:

```ts
expect(appCss).not.toContain('.mobile-action-trigger')
expect(appCss).not.toContain('.mobile-action-drawer')
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/views/TerminalView.test.ts src/components/terminal/DisplayMenu.test.ts src/components/terminal/TmuxControls.test.ts src/test/responsive-contract.test.ts
```

Expected: FAIL because the lock control is absent, menu triggers lack the new labels, and the mobile drawer still exists.

- [ ] **Step 3: Add lock state and the fourth titlebar icon**

In `TerminalView.vue`, add state beside `openMenu`:

```ts
const touchControlLocked = ref(false)
```

Bind it to `TerminalTitlebar`:

```vue
<TerminalTitlebar
  ...
  v-model:touch-control-locked="touchControlLocked"
>
```

In `TerminalTitlebar.vue`, extend props and emits:

```ts
const props = withDefaults(defineProps<{
  title: string
  computerName?: string
  status?: TerminalConnectionStatus
  displayMode: DisplayMode
  displayMenuOpen?: boolean
  touchControlLocked?: boolean
}>(), {
  computerName: 'Computer 未报告',
  status: 'connecting',
  displayMenuOpen: false,
  touchControlLocked: false,
})

const emit = defineEmits<{
  'update:displayMode': [mode: DisplayMode]
  'update:displayMenuOpen': [open: boolean]
  'update:touchControlLocked': [locked: boolean]
  rename: [name: string]
}>()
```

After the titlebar slot, render the selected fourth icon:

```vue
<button
  data-action="toggle-touch-lock"
  class="titlebar-button touch-lock-button"
  type="button"
  :aria-label="touchControlLocked ? '解除画布锁定' : '锁定画布'"
  :title="touchControlLocked ? '解除画布锁定' : '锁定画布'"
  :aria-pressed="touchControlLocked"
  @click="emit('update:touchControlLocked', !touchControlLocked)"
>
  <Lock v-if="touchControlLocked" :size="16" aria-hidden="true" />
  <Unlock v-else :size="16" aria-hidden="true" />
</button>
```

Import `Lock` and `Unlock` from `@lucide/vue`.

- [ ] **Step 4: Make the three existing triggers icon-only at the mobile breakpoint**

Give `DisplayMenu.vue`, `PaneFocusMenu.vue`, and `TmuxActionMenu.vue` explicit accessible names and wrap visible labels:

```vue
aria-label="显示设置"
<span class="control-label">显示</span>
```

```vue
aria-label="聚焦 Pane"
<span class="control-label">聚焦 Pane</span>
```

```vue
aria-label="tmux 操作"
<span class="control-label">tmux 操作</span>
```

Keep each Lucide icon, its controlled `aria-expanded`, and desktop chevron. Remove the `mobileTrigger`, `drawerOpen`, swipe handlers, mobile `<aside>`, and all drawer-focus branches from `TmuxActionMenu.vue`; `choose()` always returns focus to `desktopTrigger`.

- [ ] **Step 5: Implement compact responsive styling**

Delete `.mobile-action-trigger` and `.mobile-action-drawer` from the shared hidden-selector rule in `app.css`, leaving only `.mobile-keybar { display: none; }`. Delete the drawer-specific responsive rules.

Inside the mobile/coarse-pointer block in `terminal-responsive.css`, use:

```css
.terminal-view { height: 100dvh; grid-template-rows: auto minmax(0, 1fr) auto; }
.terminal-titlebar { gap: var(--space-1); padding-inline: max(var(--space-2), env(safe-area-inset-left)) max(var(--space-2), env(safe-area-inset-right)); }
.terminal-identity { flex: 1 1 auto; gap: var(--space-1); }
.terminal-titlebar-actions { flex: 0 0 auto; gap: var(--space-1); }
.control-label, .menu-chevron, [data-computer-name] { display: none; }
.titlebar-button { width: 2.35rem; min-width: 2.35rem; padding: 0; justify-content: center; }
.touch-lock-button[aria-pressed='true'] { border-color: var(--color-accent); background: var(--color-accent); color: var(--color-accent-contrast); }
.desktop-action-menu { display: block; }
```

Keep status visually hidden and keep floating menus layered above the terminal.

- [ ] **Step 6: Run focused tests and typecheck**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/views/TerminalView.test.ts src/components/terminal/DisplayMenu.test.ts src/components/terminal/TmuxControls.test.ts src/test/responsive-contract.test.ts
npm run typecheck
```

Expected: all focused tests pass; mobile drawer selectors are absent; TypeScript reports no errors.

- [ ] **Step 7: Commit the consolidated mobile titlebar**

```bash
git add apps/clients/web/src/views/TerminalView.vue apps/clients/web/src/views/TerminalView.test.ts apps/clients/web/src/components/terminal/TerminalTitlebar.vue apps/clients/web/src/components/terminal/DisplayMenu.vue apps/clients/web/src/components/terminal/DisplayMenu.test.ts apps/clients/web/src/components/terminal/TmuxActionMenu.vue apps/clients/web/src/components/terminal/PaneFocusMenu.vue apps/clients/web/src/components/terminal/TmuxControls.test.ts apps/clients/web/src/styles/app.css apps/clients/web/src/styles/terminal-responsive.css apps/clients/web/src/test/responsive-contract.test.ts
git commit -m "feat(web): consolidate mobile terminal controls"
```

### Task 3: Add a public xterm mouse-dispatch boundary

**Files:**

- Modify: `apps/clients/web/src/terminal/terminalAdapter.test.ts`
- Modify: `apps/clients/web/src/terminal/terminalAdapter.ts`
- Modify: `apps/clients/web/src/composables/useTerminalSession.ts`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.test.ts`

- [ ] **Step 1: Write failing pure adapter tests**

Extend `terminalAdapter.test.ts`:

```ts
import { forceSelectionModifiers } from './terminalAdapter'

it('uses the same force-selection modifier that xterm expects on each platform', () => {
  expect(forceSelectionModifiers('Linux x86_64', true)).toEqual({ shiftKey: true })
  expect(forceSelectionModifiers('iPhone', true)).toEqual({ shiftKey: true })
  expect(forceSelectionModifiers('MacIntel', true)).toEqual({ altKey: true })
  expect(forceSelectionModifiers('MacIntel', false)).toEqual({})
})
```

Add `dispatchMouse: vi.fn()` to every fake `TerminalAdapter` in `TerminalCanvas.test.ts`; this makes the interface change explicit before production code is written.

- [ ] **Step 2: Run the adapter and canvas tests and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/terminal/terminalAdapter.test.ts src/components/terminal/TerminalCanvas.test.ts
```

Expected: FAIL because `forceSelectionModifiers` and `dispatchMouse` do not exist.

- [ ] **Step 3: Define the mouse dispatch contract**

Add to `terminalAdapter.ts`:

```ts
export type TerminalMouseEventType = 'mousedown' | 'mousemove' | 'mouseup'

export interface TerminalMouseDispatch {
  type: TerminalMouseEventType
  clientX: number
  clientY: number
  buttons: 0 | 1
  button: 0
  detail?: 1 | 2
  forceSelection?: boolean
}

const MAC_PLATFORMS = new Set(['Macintosh', 'MacIntel', 'MacPPC', 'Mac68K'])

export function forceSelectionModifiers(platform: string, mouseTrackingActive: boolean): Pick<MouseEventInit, 'altKey' | 'shiftKey'> {
  if (!mouseTrackingActive) return {}
  return MAC_PLATFORMS.has(platform) ? { altKey: true } : { shiftKey: true }
}
```

Add this operation to `TerminalAdapter`:

```ts
dispatchMouse(event: TerminalMouseDispatch): void
```

- [ ] **Step 4: Dispatch through xterm's normal DOM event surface**

Set `macOptionClickForcesSelection: true` when constructing `Terminal`. Implement:

```ts
dispatchMouse: (event) => {
  const element = terminal.element
  if (!element) return
  const mouseTrackingActive = terminal.modes.mouseTrackingMode !== 'none'
  const modifiers = event.forceSelection
    ? forceSelectionModifiers(navigator.platform, mouseTrackingActive)
    : {}
  const target: EventTarget = event.type === 'mousedown' ? element : element.ownerDocument
  target.dispatchEvent(new MouseEvent(event.type, {
    bubbles: true,
    cancelable: true,
    clientX: event.clientX,
    clientY: event.clientY,
    buttons: event.buttons,
    button: event.button,
    detail: event.detail ?? 1,
    ...modifiers,
  }))
},
```

Change `canClientPan` to depend only on selection:

```ts
canClientPan: () => !terminal.hasSelection(),
```

This allows unlocked local pan even while tmux has mouse reporting enabled; actual touch compatibility mouse events are suppressed later in `TerminalCanvas`.

- [ ] **Step 5: Expose the operation through the session**

Add to the returned object in `useTerminalSession.ts`:

```ts
dispatchMouse: (event: TerminalMouseDispatch) => adapter?.dispatchMouse(event),
```

Import `TerminalMouseDispatch` as a type from `terminalAdapter.ts`.

- [ ] **Step 6: Run focused tests and typecheck**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/terminal/terminalAdapter.test.ts src/components/terminal/TerminalCanvas.test.ts
npm run typecheck
```

Expected: tests pass and every fake adapter implements the new method.

- [ ] **Step 7: Commit the adapter boundary**

```bash
git add apps/clients/web/src/terminal/terminalAdapter.ts apps/clients/web/src/terminal/terminalAdapter.test.ts apps/clients/web/src/composables/useTerminalSession.ts apps/clients/web/src/components/terminal/TerminalCanvas.test.ts
git commit -m "feat(web): expose xterm touch mouse dispatch"
```

### Task 4: Implement the touch gesture state machine

**Files:**

- Create: `apps/clients/web/src/composables/useTerminalTouchGestures.ts`
- Create: `apps/clients/web/src/composables/useTerminalTouchGestures.test.ts`

- [ ] **Step 1: Write failing state-machine tests**

Create `useTerminalTouchGestures.test.ts` with four independent cases:

```ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTerminalTouchGestures } from './useTerminalTouchGestures'

const point = (pointerId: number, x: number, y: number) => ({ pointerId, x, y })

function harness(locked = false) {
  let currentLocked = locked
  const viewport = { pointerDown: vi.fn(), pointerMove: vi.fn(), pointerUp: vi.fn() }
  const dispatchMouse = vi.fn()
  const gestures = createTerminalTouchGestures({
    locked: () => currentLocked,
    connected: () => true,
    viewport,
    dispatchMouse,
    longPressMs: 500,
    moveSlop: 8,
  })
  return { gestures, viewport, dispatchMouse, setLocked: (value: boolean) => { currentLocked = value } }
}

afterEach(() => vi.useRealTimers())

describe('terminal touch gestures', () => {
  it('delegates unlocked one- and two-pointer gestures without terminal mouse events', () => {
    const { gestures, viewport, dispatchMouse } = harness(false)
    gestures.pointerDown(point(1, 100, 200))
    gestures.pointerDown(point(2, 200, 200))
    gestures.pointerMove(point(2, 240, 200))
    gestures.pointerUp(1)
    gestures.pointerUp(2)
    expect(viewport.pointerDown).toHaveBeenCalledTimes(2)
    expect(viewport.pointerMove).toHaveBeenCalledWith(point(2, 240, 200))
    expect(dispatchMouse).not.toHaveBeenCalled()
  })

  it('turns a locked tap and drag into complete left-button lifecycles', () => {
    const { gestures, dispatchMouse } = harness(true)
    gestures.pointerDown(point(1, 50, 60))
    gestures.pointerUp(1, point(1, 50, 60))
    expect(dispatchMouse.mock.calls.map(([event]) => event.type)).toEqual(['mousedown', 'mouseup'])
    dispatchMouse.mockClear()
    gestures.pointerDown(point(2, 70, 80))
    gestures.pointerMove(point(2, 90, 80))
    gestures.pointerUp(2, point(2, 100, 80))
    expect(dispatchMouse.mock.calls.map(([event]) => event.type)).toEqual(['mousedown', 'mousemove', 'mouseup'])
  })

  it('turns a locked long press into word selection without a remote mouse down', () => {
    vi.useFakeTimers()
    const { gestures, dispatchMouse } = harness(true)
    gestures.pointerDown(point(1, 50, 60))
    vi.advanceTimersByTime(500)
    expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mousedown', detail: 2, forceSelection: true }))
    gestures.pointerMove(point(1, 100, 60))
    gestures.pointerUp(1, point(1, 100, 60))
    expect(dispatchMouse.mock.calls.every(([event]) => event.forceSelection === true)).toBe(true)
  })

  it('cancels timers and releases an active button on a second touch or mode change', () => {
    vi.useFakeTimers()
    const { gestures, dispatchMouse, setLocked } = harness(true)
    gestures.pointerDown(point(1, 10, 10))
    gestures.pointerMove(point(1, 30, 10))
    gestures.pointerDown(point(2, 40, 10))
    expect(dispatchMouse.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ type: 'mouseup', buttons: 0 }))
    setLocked(false)
    gestures.cancelAll()
    vi.runAllTimers()
    expect(vi.getTimerCount()).toBe(0)
  })
})
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/composables/useTerminalTouchGestures.test.ts
```

Expected: FAIL because the composable does not exist.

- [ ] **Step 3: Implement the explicit state machine**

Create `useTerminalTouchGestures.ts` with the complete implementation below:

```ts
import type { PointerSample } from './usePointerViewport'
import type { TerminalMouseDispatch } from '../terminal/terminalAdapter'

interface ViewportPointerSink {
  pointerDown(point: PointerSample): void
  pointerMove(point: PointerSample): void
  pointerUp(pointerId: number): void
}

interface TerminalTouchGestureOptions {
  locked(): boolean
  connected(): boolean
  viewport: ViewportPointerSink
  dispatchMouse(event: TerminalMouseDispatch): void
  longPressMs?: number
  moveSlop?: number
}

type LockedPhase = 'pending' | 'remote' | 'selection'

interface LockedGesture {
  pointerId: number
  start: PointerSample
  current: PointerSample
  phase: LockedPhase
  timer: ReturnType<typeof setTimeout> | null
}

const mouse = (type: TerminalMouseDispatch['type'], point: PointerSample, forceSelection = false, detail: 1 | 2 = 1): TerminalMouseDispatch => ({
  type,
  clientX: point.x,
  clientY: point.y,
  buttons: type === 'mouseup' ? 0 : 1,
  button: 0,
  detail,
  forceSelection,
})

export function createTerminalTouchGestures(options: TerminalTouchGestureOptions) {
  const longPressMs = options.longPressMs ?? 500
  const moveSlop = options.moveSlop ?? 8
  const viewportPointers = new Set<number>()
  const lockedPointers = new Set<number>()
  let active: LockedGesture | null = null
  let lockedBlocked = false

  function clearTimer() {
    if (active?.timer !== null && active?.timer !== undefined) clearTimeout(active.timer)
    if (active) active.timer = null
  }

  function finishLocked(end = active?.current) {
    if (!active || !end) return
    clearTimer()
    if (active.phase === 'remote' || active.phase === 'selection') {
      const selecting = active.phase === 'selection'
      options.dispatchMouse(mouse('mouseup', end, selecting, selecting ? 2 : 1))
    }
    active = null
  }

  function pointerDown(point: PointerSample) {
    if (!options.locked() || !options.connected()) {
      viewportPointers.add(point.pointerId)
      options.viewport.pointerDown(point)
      return
    }
    lockedPointers.add(point.pointerId)
    if (lockedBlocked) return
    if (active) {
      finishLocked()
      lockedBlocked = true
      return
    }
    active = { pointerId: point.pointerId, start: point, current: point, phase: 'pending', timer: null }
    active.timer = setTimeout(() => {
      if (!active || active.phase !== 'pending') return
      active.phase = 'selection'
      options.dispatchMouse(mouse('mousedown', active.start, true, 2))
    }, longPressMs)
  }

  function pointerMove(point: PointerSample) {
    if (viewportPointers.has(point.pointerId)) {
      options.viewport.pointerMove(point)
      return
    }
    if (lockedBlocked || !active || active.pointerId !== point.pointerId) return
    active.current = point
    if (active.phase === 'pending') {
      if (Math.hypot(point.x - active.start.x, point.y - active.start.y) < moveSlop) return
      clearTimer()
      active.phase = 'remote'
      options.dispatchMouse(mouse('mousedown', active.start))
      options.dispatchMouse(mouse('mousemove', point))
      return
    }
    const selecting = active.phase === 'selection'
    options.dispatchMouse(mouse('mousemove', point, selecting, selecting ? 2 : 1))
  }

  function pointerUp(pointerId: number, point?: PointerSample) {
    if (viewportPointers.delete(pointerId)) {
      options.viewport.pointerUp(pointerId)
      return
    }
    lockedPointers.delete(pointerId)
    if (lockedBlocked) {
      if (lockedPointers.size === 0) lockedBlocked = false
      return
    }
    if (!active || active.pointerId !== pointerId) return
    const end = point ?? active.current
    if (active.phase === 'pending') {
      clearTimer()
      options.dispatchMouse(mouse('mousedown', end))
      options.dispatchMouse(mouse('mouseup', end))
      active = null
      return
    }
    finishLocked(end)
  }

  function pointerCancel(pointerId: number, point?: PointerSample) {
    if (viewportPointers.delete(pointerId)) {
      options.viewport.pointerUp(pointerId)
      return
    }
    lockedPointers.delete(pointerId)
    if (active?.pointerId === pointerId) finishLocked(point ?? active.current)
    if (lockedPointers.size === 0) lockedBlocked = false
  }

  function cancelAll() {
    for (const pointerId of viewportPointers) options.viewport.pointerUp(pointerId)
    viewportPointers.clear()
    finishLocked()
    lockedPointers.clear()
    lockedBlocked = false
  }

  return { pointerDown, pointerMove, pointerUp, pointerCancel, cancelAll, dispose: cancelAll }
}
```

- [ ] **Step 4: Run the composable tests and verify GREEN**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/composables/useTerminalTouchGestures.test.ts
npm run typecheck
```

Expected: four gesture tests pass, no timers remain, and TypeScript reports no errors.

- [ ] **Step 5: Commit the isolated gesture unit**

```bash
git add apps/clients/web/src/composables/useTerminalTouchGestures.ts apps/clients/web/src/composables/useTerminalTouchGestures.test.ts
git commit -m "feat(web): add mobile terminal gesture arbiter"
```

### Task 5: Wire lock mode and gestures into the terminal canvas

**Files:**

- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Modify: `apps/clients/web/src/views/TerminalView.vue`

- [ ] **Step 1: Write failing canvas routing tests**

Add this focused setup helper to `TerminalCanvas.test.ts`:

```ts
function mountTouchCanvas(touchControlLocked: boolean) {
  let callbacks!: TerminalSocketCallbacks
  const socket: TerminalSocketLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
  const dispatchMouse = vi.fn()
  const adapter: TerminalAdapter = {
    write: vi.fn(),
    resize: vi.fn(),
    reset: vi.fn(),
    focus: vi.fn(),
    refreshTheme: vi.fn(),
    setInputEnabled: vi.fn(),
    measureCell: vi.fn(() => ({ width: 10, height: 20 })),
    setVisualScale: vi.fn((scale: number) => ({ width: 10 * scale, height: 20 * scale })),
    dispatchMouse,
    canClientPan: vi.fn(() => true),
    dispose: vi.fn(),
  }
  const createSocket = vi.fn((_id: string, nextCallbacks: TerminalSocketCallbacks) => {
    callbacks = nextCallbacks
    return socket
  })
  const createAdapter: TerminalAdapterFactory = vi.fn(() => adapter)
  const wrapper = mount(TerminalCanvas, {
    props: { termId: 'term-touch', touchControlLocked, createSocket, createAdapter },
  })
  const ready = () => callbacks.onReady({
    type: 'terminal.ready',
    terminal_id: 't1',
    stream_id: 's1',
    rows: 40,
    cols: 120,
  })
  return { wrapper, adapter, dispatchMouse, socket, callbacks, ready }
}
```

Extend the file with two tests. The unlocked case must preserve local presentation and suppress terminal input:

```ts
it('routes unlocked touch only to local pan and pinch', async () => {
  const { wrapper, dispatchMouse, socket, ready } = mountTouchCanvas(false)
  ready()
  await flushPromises()
  const frame = wrapper.get('.terminal-frame')
  await frame.trigger('pointerdown', { pointerId: 1, pointerType: 'touch', clientX: 200, clientY: 200 })
  await frame.trigger('pointermove', { pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200 })
  await frame.trigger('pointerup', { pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200 })
  expect(dispatchMouse).not.toHaveBeenCalled()
  expect(socket.sendInput).not.toHaveBeenCalled()
  expect(wrapper.vm.captureViewport().panX).toBeLessThan(0)
})
```

The locked case must exercise tap, drag, long press, cancellation, and disconnect fallback:

```ts
it('routes locked touch through xterm and preserves long-press selection', async () => {
  vi.useFakeTimers()
  const { wrapper, dispatchMouse, callbacks, ready } = mountTouchCanvas(true)
  ready()
  callbacks.onStatus('connected')
  await flushPromises()
  const frame = wrapper.get('.terminal-frame')

  await frame.trigger('pointerdown', { pointerId: 1, pointerType: 'touch', clientX: 50, clientY: 60 })
  await frame.trigger('pointerup', { pointerId: 1, pointerType: 'touch', clientX: 50, clientY: 60 })
  expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mousedown', forceSelection: false }))
  expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mouseup', buttons: 0 }))

  dispatchMouse.mockClear()
  await frame.trigger('pointerdown', { pointerId: 2, pointerType: 'touch', clientX: 80, clientY: 90 })
  vi.advanceTimersByTime(500)
  expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ detail: 2, forceSelection: true }))
  await frame.trigger('pointercancel', { pointerId: 2, pointerType: 'touch', clientX: 80, clientY: 90 })
  expect(dispatchMouse.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ type: 'mouseup' }))

  callbacks.onStatus('reconnecting')
  dispatchMouse.mockClear()
  await frame.trigger('pointerdown', { pointerId: 3, pointerType: 'touch', clientX: 100, clientY: 120 })
  await frame.trigger('pointermove', { pointerId: 3, pointerType: 'touch', clientX: 70, clientY: 120 })
  expect(dispatchMouse).not.toHaveBeenCalled()
  vi.useRealTimers()
})
```

- [ ] **Step 2: Run the canvas tests and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/components/terminal/TerminalCanvas.test.ts
```

Expected: FAIL because `touchControlLocked` is not a prop and touch still goes directly to `usePointerViewport`.

- [ ] **Step 3: Integrate the gesture arbiter**

Add `touchControlLocked?: boolean` to `TerminalCanvas` props with default `false`. Import and create the arbiter:

```ts
const pointer = createPointerViewport({
  viewport: frame.value,
  content: frame.value,
  canPan: () => !props.selectionActive && session.canClientPan(),
})

const touchGestures = createTerminalTouchGestures({
  locked: () => props.touchControlLocked,
  connected: () => status.value === 'connected',
  viewport: pointer,
  dispatchMouse: session.dispatchMouse,
})
```

Replace direct pointer routing with:

```ts
function onPointerDown(event: PointerEvent) {
  if (event.pointerType === 'mouse') return
  event.preventDefault()
  frameElement.value?.setPointerCapture?.(event.pointerId)
  touchGestures.pointerDown(point(event))
}

function onPointerMove(event: PointerEvent) {
  if (event.pointerType === 'mouse') return
  event.preventDefault()
  touchGestures.pointerMove(point(event))
}

function onPointerUp(event: PointerEvent) {
  if (event.pointerType === 'mouse') return
  event.preventDefault()
  touchGestures.pointerUp(event.pointerId, point(event))
  frameElement.value?.releasePointerCapture?.(event.pointerId)
}

function onPointerCancel(event: PointerEvent) {
  if (event.pointerType === 'mouse') return
  touchGestures.pointerCancel(event.pointerId, point(event))
  frameElement.value?.releasePointerCapture?.(event.pointerId)
}
```

Bind `@pointercancel="onPointerCancel"` separately. Add `:data-touch-control="touchControlLocked ? 'locked' : 'viewport'"` to `.terminal-frame`.

Watch lock and connection transitions, and clean up:

```ts
watch([() => props.touchControlLocked, status], () => touchGestures.cancelAll())
onBeforeUnmount(() => {
  observer?.disconnect()
  touchGestures.dispose()
})
```

Consolidate the existing observer-only unmount hook into this one; do not register two unmount callbacks for the same canvas resources.

- [ ] **Step 4: Pass the view-owned state to the canvas**

In `TerminalView.vue`:

```vue
<TerminalCanvas
  ...
  :touch-control-locked="touchControlLocked"
/>
```

Do not store this value in `orientationViews`; the parent ref naturally survives orientation changes and resets on a fresh mount.

- [ ] **Step 5: Run gesture, canvas, and view tests**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/composables/useTerminalTouchGestures.test.ts src/components/terminal/TerminalCanvas.test.ts src/views/TerminalView.test.ts
npm run typecheck
```

Expected: unlocked touch emits no terminal mouse/input; locked tap/drag emits complete mouse lifecycles; long press uses only force-selection events; reconnecting falls back to local viewport routing.

- [ ] **Step 6: Commit canvas integration**

First inspect the canvas diff against the clean feature-branch baseline:

```bash
git diff -- apps/clients/web/src/components/terminal/TerminalCanvas.vue apps/clients/web/src/components/terminal/TerminalCanvas.test.ts apps/clients/web/e2e/control-center.spec.ts
```

Then stage only this task's canvas/view files:

```bash
git add apps/clients/web/src/components/terminal/TerminalCanvas.vue apps/clients/web/src/components/terminal/TerminalCanvas.test.ts apps/clients/web/src/views/TerminalView.vue
git commit -m "feat(web): switch mobile terminal touch modes"
```

### Task 6: Reserve a real bottom row for mobile keys

**Files:**

- Modify: `apps/clients/web/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/src/styles/terminal-responsive.css`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write the failing bottom-row CSS contract**

Replace the old fixed-keybar assertion in `responsive-contract.test.ts` with:

```ts
expect(css).toContain('.terminal-view { height: 100dvh; grid-template-rows: auto minmax(0, 1fr) auto; }')
expect(css).toContain('.mobile-keybar { position: static; grid-row: 3;')
expect(css).toContain('padding-block-end: max(var(--space-2), env(safe-area-inset-bottom))')
expect(css).not.toContain('.mobile-keybar { position: fixed')
expect(css).not.toContain('inset-block-end: env(safe-area-inset-bottom)')
```

- [ ] **Step 2: Run the responsive test and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/test/responsive-contract.test.ts
```

Expected: FAIL because the keybar is still fixed over the terminal.

- [ ] **Step 3: Move the keybar into normal grid layout**

Keep the base `.terminal-view` two-row desktop grid in `app.css`. In the mobile/coarse-pointer block, override it and replace the current keybar rule with:

```css
.terminal-view { height: 100dvh; grid-template-rows: auto minmax(0, 1fr) auto; }
.terminal-frame { grid-row: 2; min-height: 0; touch-action: none; }
.mobile-keybar {
  position: static;
  z-index: 40;
  grid-row: 3;
  min-width: 0;
  display: flex;
  gap: var(--space-1);
  overflow-x: auto;
  padding: var(--space-2) max(var(--space-2), env(safe-area-inset-right)) max(var(--space-2), env(safe-area-inset-bottom)) max(var(--space-2), env(safe-area-inset-left));
  border-block-start: 1px solid var(--color-border);
  background: var(--color-panel);
}
```

Do not add bottom padding to the terminal frame. The keybar owns the safe area and the terminal frame ends above it.

- [ ] **Step 4: Run focused layout tests and build**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/test/responsive-contract.test.ts src/components/terminal/MobileKeyBar.test.ts src/views/TerminalView.test.ts
npm run build
```

Expected: tests pass and Vite produces `dist/` without type or CSS errors.

- [ ] **Step 5: Commit the stable bottom row**

```bash
git add apps/clients/web/src/styles/app.css apps/clients/web/src/styles/terminal-responsive.css apps/clients/web/src/test/responsive-contract.test.ts
git commit -m "fix(web): reserve mobile terminal key row"
```

### Task 7: Prove the behavior against disposable real tmux

**Files:**

- Modify: `apps/clients/web/e2e/control-center.spec.ts`
- Modify: `docs/web-client.md`

- [ ] **Step 1: Add a Chromium touch helper to the E2E test**

Add a helper that holds one CDP session for a complete gesture, with touch points expressed in page CSS coordinates:

```ts
interface TouchPoint { x: number; y: number; id: number }
interface TouchStep {
  type: 'touchStart' | 'touchMove' | 'touchEnd'
  points: TouchPoint[]
  delayMs?: number
}

async function dispatchTouchSequence(page: Page, steps: TouchStep[]) {
  const session = await page.context().newCDPSession(page)
  try {
    for (const step of steps) {
      await session.send('Input.dispatchTouchEvent', {
        type: step.type,
        touchPoints: step.points.map((point) => ({ x: point.x, y: point.y, id: point.id })),
      })
      if (step.delayMs) await page.waitForTimeout(step.delayMs)
    }
  } finally {
    await session.detach()
  }
}
```

- [ ] **Step 2: Replace stale mobile drawer and Computer assertions**

For desktop, retain the Computer/Term baseline alignment assertion. For both mobile projects, assert:

```ts
await expect(computerName).toBeHidden()
await expect(page.getByRole('button', { name: '显示设置' })).toBeVisible()
await expect(page.getByRole('button', { name: '聚焦 Pane' })).toBeVisible()
await expect(page.getByRole('button', { name: 'tmux 操作' })).toBeVisible()
await expect(page.getByRole('button', { name: '锁定画布' })).toHaveAttribute('aria-pressed', 'false')
await expect(page.getByRole('button', { name: '快捷操作' })).toHaveCount(0)
await expect(page.getByLabel('移动端 Tmux 操作')).toHaveCount(0)
```

Open the titlebar tmux menu on mobile and exercise one non-destructive semantic action through the same menu used on desktop.

Add this deterministic helper before the main test so every Playwright project establishes its own required Pane topology:

```ts
async function ensureTwoPanes(page: Page): Promise<[PaneGeometry, PaneGeometry]> {
  let panes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
  if (panes.length === 1) {
    await page.getByRole('button', { name: 'tmux 操作' }).click()
    await page.getByRole('menuitem', { name: /左右切分 Pane/ }).click()
    await expect.poll(async () => (await panesForTerm(page)).length).toBe(2)
    panes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
  }
  expect(panes).toHaveLength(2)
  return [panes[0]!, panes[1]!]
}
```

- [ ] **Step 3: Add unlocked pan/pinch acceptance**

In mobile projects, select `100% 实际字号`, then record `terminalFrames.length`, `.terminal-grid` transform, and `data-visual-scale`. Define the gesture center from the terminal frame:

```ts
await page.getByRole('button', { name: '显示设置' }).click()
await page.getByRole('menuitemradio', { name: '100% 实际字号' }).click()
const frameBox = await page.locator('.terminal-frame').boundingBox()
expect(frameBox).not.toBeNull()
const center = { x: frameBox!.x + frameBox!.width / 2, y: frameBox!.y + frameBox!.height / 2 }
const frameCountBefore = terminalFrames.length
const transformBefore = await page.locator('.terminal-grid').evaluate((element) => getComputedStyle(element).transform)
const scaleBefore = Number(await page.locator('.terminal-frame').getAttribute('data-visual-scale'))
```

Send a one-finger drag and a two-finger pinch through CDP. Assert:

```ts
expect(await page.locator('.terminal-grid').evaluate((element) => getComputedStyle(element).transform)).not.toBe(transformBefore)
expect(Number(await page.locator('.terminal-frame').getAttribute('data-visual-scale'))).toBeGreaterThan(scaleBefore)
expect(terminalFrames).toHaveLength(frameCountBefore)
```

Switch Display back to `适应窗口` before remote mouse acceptance so local pan/zoom is reset.

Use these concrete sequences:

```ts
await dispatchTouchSequence(page, [
  { type: 'touchStart', points: [{ id: 1, x: center.x + 40, y: center.y }] },
  { type: 'touchMove', points: [{ id: 1, x: center.x - 40, y: center.y }] },
  { type: 'touchEnd', points: [] },
])

await dispatchTouchSequence(page, [
  { type: 'touchStart', points: [
    { id: 1, x: center.x - 30, y: center.y },
    { id: 2, x: center.x + 30, y: center.y },
  ] },
  { type: 'touchMove', points: [
    { id: 1, x: center.x - 70, y: center.y },
    { id: 2, x: center.x + 70, y: center.y },
  ] },
  { type: 'touchEnd', points: [] },
])
```

- [ ] **Step 4: Add locked tap/drag and long-press selection acceptance**

Call `ensureTwoPanes(page)`, turn lock on, and assert `aria-pressed="true"`. Touch the visual center of the inactive Pane, then poll `/topology` until it becomes active. Start a second touch in that Pane, move more than eight CSS pixels, and end it; inspect captured WebSocket frames for a complete SGR down/move/up sequence at the visual target.

Use the existing `terminalPoint()` result and these assertions:

```ts
const frameStart = terminalFrames.length
await dispatchTouchSequence(page, [
  { type: 'touchStart', points: [{ id: 1, x: target.x, y: target.y }] },
  { type: 'touchEnd', points: [] },
])
await expect.poll(async () => (await panesForTerm(page)).find((pane) => pane.active)?.pane_id).toBe(inactivePane.pane_id)

await dispatchTouchSequence(page, [
  { type: 'touchStart', points: [{ id: 2, x: target.x, y: target.y }] },
  { type: 'touchMove', points: [{ id: 2, x: target.x + 20, y: target.y }] },
  { type: 'touchEnd', points: [] },
])
const mousePayload = Buffer.concat(terminalFrames.slice(frameStart)).toString('latin1')
expect(mousePayload).toMatch(/\x1b\[<0;\d+;\d+M/)
expect(mousePayload).toMatch(/\x1b\[<32;\d+;\d+M/)
expect(mousePayload).toMatch(/\x1b\[<0;\d+;\d+m/)
```

Write two unique words to the terminal, record the frame count, hold one touch over the second word for at least 550ms, drag across the word, and release. Copy from `.xterm` through the existing `ClipboardEvent` helper and assert the selected text contains only the second word. Assert the long-press sequence added no terminal input frames.

```ts
const firstWord = `LEFTMOBILE${Date.now().toString(36)}`
const secondWord = `RIGHTMOBILE${Date.now().toString(36)}`
await page.locator('.terminal-host textarea').focus()
await page.keyboard.type(String.raw`printf '\033[2J\033[H${firstWord}    ${secondWord}'`)
await page.keyboard.press('Enter')
await expect(page.locator('.terminal-host')).toContainText(secondWord)
const activePane = (await panesForTerm(page)).find((pane) => pane.active)!
const wordStart = await terminalPoint(page, activePane.left + firstWord.length + 4, activePane.top)
const wordEnd = await terminalPoint(page, activePane.left + firstWord.length + 4 + secondWord.length - 1, activePane.top)
const selectionFrameStart = terminalFrames.length
await dispatchTouchSequence(page, [
  { type: 'touchStart', points: [{ id: 3, x: wordStart.x, y: wordStart.y }], delayMs: 550 },
  { type: 'touchMove', points: [{ id: 3, x: wordEnd.x, y: wordEnd.y }] },
  { type: 'touchEnd', points: [] },
])
const copied = await page.locator('.xterm').evaluate((element) => {
  const clipboard = new DataTransfer()
  element.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: clipboard }))
  return clipboard.getData('text/plain')
})
expect(copied).toBe(secondWord)
expect(terminalFrames).toHaveLength(selectionFrameStart)
```

- [ ] **Step 5: Prove the bottom row covers the viewport edge**

In portrait and landscape, collect:

```ts
const mobileLayout = await page.evaluate(() => {
  const view = document.querySelector<HTMLElement>('.terminal-view')!
  const frame = document.querySelector<HTMLElement>('.terminal-frame')!
  const keybar = document.querySelector<HTMLElement>('.mobile-keybar')!
  const frameBox = frame.getBoundingClientRect()
  const keybarBox = keybar.getBoundingClientRect()
  return {
    viewOverflow: view.scrollHeight - view.clientHeight,
    frameBottom: frameBox.bottom,
    keybarTop: keybarBox.top,
    keybarBottom: keybarBox.bottom,
    keybarPosition: getComputedStyle(keybar).position,
    viewportHeight: window.innerHeight,
  }
})
expect(mobileLayout.viewOverflow).toBeLessThanOrEqual(1)
expect(mobileLayout.frameBottom).toBeLessThanOrEqual(mobileLayout.keybarTop + 1)
expect(mobileLayout.keybarBottom).toBeLessThanOrEqual(mobileLayout.viewportHeight + 1)
expect(mobileLayout.keybarPosition).toBe('static')
```

Capture portrait and landscape screenshots after these assertions.

- [ ] **Step 6: Update current behavior documentation**

Replace the stale mobile paragraph in `docs/web-client.md` with the exact contract:

```md
手机端隐藏 Computer 名称，并在右上使用“显示”“聚焦 Pane”“tmux 操作”和“锁定画布”四个 SVG 图标。锁定默认关闭：单指平移本地画布，双指缩放，且不产生终端鼠标输入；锁定开启后，轻点和拖动通过 xterm 发送 tmux 鼠标，长按约 500ms 进入 xterm 文本选择。旋转屏幕保留当前锁定状态，离开并重新进入 Term 后恢复未锁定。底部 Ctrl、Alt、Shift、Esc、Tab 和 Prefix 是独立布局行，不覆盖 terminal 画布。
```

- [ ] **Step 7: Run the isolated browser suite**

Run from repository root:

```bash
./scripts/run-web-e2e.sh
```

Expected: desktop, mobile portrait, and mobile landscape pass against one disposable Computer/Term; the runner removes its exact temporary resources on success and prints the evidence directory on failure. It must not use the existing live service, database, tmux socket, Docker container, or port.

- [ ] **Step 8: Commit browser acceptance and docs**

```bash
git add apps/clients/web/e2e/control-center.spec.ts docs/web-client.md
git commit -m "test(web): verify mobile terminal touch controls"
```

### Task 8: Run full gates and prepare integration

**Files:**

- Verify only; modify only files already listed if a gate exposes a feature regression.

- [ ] **Step 1: Run all Web gates fresh**

```bash
cd apps/clients/web
npm run test:run
npm run typecheck
npm run build
```

Expected: all Vitest files pass, `vue-tsc` reports no errors, and Vite creates production assets.

- [ ] **Step 2: Run repository verification**

From the isolated worktree root:

```bash
./scripts/verify.sh
```

Expected: protocol, Node, Control Plane, cross-process, docs, deployment, Ruff, mypy, Web tests, typecheck, and build all pass with zero failures.

- [ ] **Step 3: Audit scope, privacy, and presentation boundaries**

```bash
git diff main...HEAD --check
git diff --stat main...HEAD
rg -n "mobile-action-trigger|mobile-action-drawer|scale\(" apps/clients/web/src
rg -n "terminal\.resize|FitAddon" apps/clients/web/src
```

Expected: no whitespace errors; no mobile drawer; no CSS scale on an ancestor of `.xterm-screen`; no Web-driven terminal resize or FitAddon; only the 22 planned files changed.

- [ ] **Step 4: Review every requirement against fresh evidence**

Confirm all of the following from test output and browser screenshots:

- mobile logout is icon-only and still named `退出登录`;
- Computer name is hidden only on mobile/coarse pointers;
- four titlebar SVG controls are reachable in portrait and landscape;
- lock defaults off, survives orientation, and resets on remount;
- unlocked pan/pinch sends no terminal input;
- locked click/drag targets the real tmux Pane;
- long press selects xterm text without remote mouse frames;
- the mobile keybar cannot reveal a black bottom gap;
- desktop menus, wording, selection, and mouse behavior remain green.

- [ ] **Step 5: Reconcile the latest main baseline**

Fetch or inspect the latest local `main`, then merge it into the feature branch. Preserve `4033ede`, `ef4168c`, and any later terminal-geometry fixes while resolving overlapping canvas/E2E hunks. Run Tasks 7 and 8 again after conflict resolution. Do not stage or commit unrelated main-worktree changes.

- [ ] **Step 6: Choose integration using the finishing skill**

Invoke `superpowers:finishing-a-development-branch`. Present merge, PR, keep-branch, or cleanup options. Do not merge, push, deploy, or remove a worktree until the user chooses the corresponding integration action.
