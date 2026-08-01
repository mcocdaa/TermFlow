# Terminal Detail Interactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all dashboard metrics equally informative and make the terminal titlebar use direct-name editing, mutually exclusive desktop menus, and hover-only tmux shortcut hints.

**Architecture:** Keep the API and terminal protocol unchanged. `DashboardView` supplies metric descriptions, `TerminalView` owns one desktop-menu identifier, the three menu components expose controlled `open` state, and `TmuxActionMenu` renders server-reported bindings in accessible tooltips.

**Tech Stack:** Vue 3.5, TypeScript, Vue Test Utils, Vitest, Playwright, existing CSS design tokens.

---

### Task 1: Give every metric card contextual help

**Files:**
- Modify: `apps/clients/web/src/views/DashboardView.vue`
- Modify: `apps/clients/web/src/views/DashboardView.test.ts`

- [ ] **Step 1: Write the failing test**

Extend the dashboard test to require all four `.metric-card` elements to have `aria-describedby` and tooltips. Assert that the 24-hour card explains its time window and the Computers card includes the current online-computer count.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:run -- src/views/DashboardView.test.ts`

Expected: FAIL because only the Online Terms and Active Panes cards currently render tooltips.

- [ ] **Step 3: Write minimal implementation**

Compute the online computer count from `snapshot.computers`, then pass `help` strings to the other two `MetricCard` instances.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:run -- src/views/DashboardView.test.ts`

Expected: PASS.

### Task 2: Use the terminal name itself as the rename control

**Files:**
- Modify: `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`
- Modify: `apps/clients/web/src/views/TerminalView.test.ts`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write the failing test**

Require `[data-term-name]` to be a button with the edit action and accessible label, require no pencil icon, and require terminal/computer identity to share one no-wrap row.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:run -- src/views/TerminalView.test.ts`

Expected: FAIL because the term name is a `strong`, the edit action is a separate icon button, and the computer name is on a second grid row.

- [ ] **Step 3: Write minimal implementation**

Replace the strong-plus-pencil pair with a button containing the term name, keep the existing inline form, place the computer name beside the control, and update CSS to use a single non-wrapping flex row with ellipsis.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:run -- src/views/TerminalView.test.ts`

Expected: PASS including the existing PATCH rename assertion.

### Task 3: Make desktop terminal menus mutually exclusive

**Files:**
- Modify: `apps/clients/web/src/views/TerminalView.vue`
- Modify: `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/PaneFocusMenu.vue`
- Modify: `apps/clients/web/src/views/TerminalView.test.ts`
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TmuxControls.test.ts`

- [ ] **Step 1: Write the failing integration test**

Open Display, then tmux, then Pane focus through `TerminalView`; after each click assert only the newest menu remains and all three triggers expose the correct `aria-expanded` state.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:run -- src/views/TerminalView.test.ts`

Expected: FAIL because each child currently owns independent local state.

- [ ] **Step 3: Write controlled-component tests**

Update component tests to pass `open`, assert `update:open` is emitted by trigger/selection/Escape, and update wrapper props to simulate parent state.

- [ ] **Step 4: Implement the minimal shared state**

Add `openMenu: 'display' | 'tmux' | 'pane' | null` to `TerminalView`; pass booleans to the three menus and translate their updates to the single identifier. Remove their independent desktop-open refs while retaining the mobile drawer ref.

- [ ] **Step 5: Run affected tests**

Run: `npm run test:run -- src/views/TerminalView.test.ts src/components/terminal/DisplayMenu.test.ts src/components/terminal/TmuxControls.test.ts`

Expected: PASS.

### Task 4: Move tmux bindings into hover/focus tooltips

**Files:**
- Modify: `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/TmuxControls.test.ts`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write the failing test**

Require each action button's direct text to contain only its function label, require no visible `small` binding, and require an associated `role="tooltip"` with a `<code>` key string such as `Ctrl + a` and an explicit `未绑定` fallback.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:run -- src/components/terminal/TmuxControls.test.ts`

Expected: FAIL because the desktop menu currently displays bindings in `<small>` elements and only uses a native `title`.

- [ ] **Step 3: Write minimal implementation**

Render the server-reported binding inside a custom tooltip, format `C-x` as `Ctrl + x`, connect it through `aria-describedby`, and add hover/focus CSS without changing semantic action dispatch.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:run -- src/components/terminal/TmuxControls.test.ts`

Expected: PASS.

### Task 5: Verify and deliver

**Files:**
- Test: `apps/clients/web/src/**/*.test.ts`
- Test: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] **Step 1: Run complete static and unit verification**

Run `npm run test:run`, `npm run typecheck`, and `npm run build` from `apps/clients/web`.

- [ ] **Step 2: Build and run a disposable Docker instance**

Build a unique control-plane image from this worktree; run it on a unique loopback port with a temporary volume/database and preserve the current `127.0.0.1:8765` container unchanged.

- [ ] **Step 3: Run real-browser acceptance**

Verify all four metric tooltips, direct terminal-name editing, single-open desktop menus, and tmux key tooltips at desktop width. Capture viewport screenshots and console errors.

- [ ] **Step 4: Re-run full verification after final edits**

Repeat the unit test, typecheck, and build commands and inspect `git diff --check`.

- [ ] **Step 5: Commit, merge, and deploy**

Commit the implementation, merge it to `main`, tag the current live image/container for rollback, deploy the exact browser-tested image, and verify health, static asset hashes, A reconnection metrics, and logs.
