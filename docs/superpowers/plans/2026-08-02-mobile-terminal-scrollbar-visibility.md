# Mobile Terminal Scrollbar Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the terminal-frame and mobile-keybar scrollbar indicators on mobile without disabling canvas navigation or keybar horizontal scrolling.

**Architecture:** Keep every existing overflow and pointer rule intact and suppress only the native scrollbar presentation inside the existing mobile/coarse-pointer media query. Lock the cross-browser CSS contract with Vitest, then verify computed styles and retained touch scrolling in the disposable real-browser fixture.

**Tech Stack:** Vue 3, TypeScript, CSS, Vitest, Playwright, Docker Compose

---

## File map

- Modify `packages/client-ui/src/test/responsive-contract.test.ts`: define the Firefox and WebKit mobile scrollbar contract while retaining overflow and touch-action assertions.
- Modify `packages/client-ui/src/styles/terminal-responsive.css`: hide only `.terminal-frame` and `.mobile-keybar` scrollbar presentation at the mobile/coarse-pointer breakpoint.
- Modify `apps/clients/web/e2e/control-center.spec.ts`: prove both target scrollbars are visually suppressed in a real browser while the keybar still scrolls horizontally.
- Verify `packages/client-ui/src/components/terminal/TerminalCanvas.vue`, `packages/client-ui/src/components/terminal/MobileKeyBar.vue`, and pointer tests without changing their behavior.

### Task 1: Specify mobile scrollbar visibility

**Files:**
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] **Step 1: Add the failing CSS contract**

In `responsive-contract.test.ts`, extend `uses one portrait/landscape logic with coarse-pointer behavior and safe-area insets` immediately after the existing `.mobile-keybar` overflow assertions:

```ts
expect(css).toMatch(
  /\.terminal-frame,\s*\.mobile-keybar\s*\{\s*scrollbar-width: none;\s*\}/,
)
expect(css).toMatch(
  /\.terminal-frame::\-webkit-scrollbar,\s*\.mobile-keybar::\-webkit-scrollbar\s*\{\s*display: none;\s*\}/,
)
expect(css).toContain('overflow-x: auto;')
expect(css).toContain('touch-action: pan-x;')
expect(appCss).not.toContain('scrollbar-width: none;')
```

The final assertion prevents this mobile-only presentation rule from leaking into the shared desktop stylesheet.

- [ ] **Step 2: Add failing real-browser style assertions**

In the non-desktop branch of `control-center.spec.ts`, extend the existing `mobileLayout` object returned from `page.evaluate`:

```ts
frameScrollbarWidth: getComputedStyle(frame).getPropertyValue('scrollbar-width'),
keybarScrollbarWidth: getComputedStyle(keybar).getPropertyValue('scrollbar-width'),
frameWebkitScrollbarDisplay: getComputedStyle(frame, '::-webkit-scrollbar').display,
keybarWebkitScrollbarDisplay: getComputedStyle(keybar, '::-webkit-scrollbar').display,
```

After the existing keybar boundary-drag assertion and before the Pane interaction sequence, add:

```ts
expect(mobileLayout.frameScrollbarWidth).toBe('none')
expect(mobileLayout.keybarScrollbarWidth).toBe('none')
expect(mobileLayout.frameWebkitScrollbarDisplay).toBe('none')
expect(mobileLayout.keybarWebkitScrollbarDisplay).toBe('none')
```

Do not remove or weaken the later `keybarScrollLeft` assertions; they prove hidden presentation does not disable horizontal touch scrolling.

- [ ] **Step 3: Run the focused contract test and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
```

Expected: FAIL because `terminal-responsive.css` does not yet contain `scrollbar-width: none` or the scoped WebKit pseudo-element rule.

- [ ] **Step 4: Run isolated browser acceptance and verify RED**

Run:

```bash
./scripts/run-web-e2e.sh
```

Expected: the desktop project remains unaffected; both mobile projects first pass the existing keybar touch-scroll checks and then FAIL on at least one new scrollbar-style assertion. Preserve the emitted temporary evidence directory until the failure is understood.

- [ ] **Step 5: Commit the tests**

```bash
git add packages/client-ui/src/test/responsive-contract.test.ts apps/clients/web/e2e/control-center.spec.ts
git commit -m "test(web): specify hidden mobile terminal scrollbars"
```

### Task 2: Hide only the two mobile scrollbar indicators

**Files:**
- Modify: `packages/client-ui/src/styles/terminal-responsive.css`
- Test: `packages/client-ui/src/test/responsive-contract.test.ts`
- Test: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] **Step 1: Add the minimal cross-browser CSS**

Inside the existing `@media (max-width: 47.99rem), (pointer: coarse)` block, immediately after the `.terminal-frame` rule, add:

```css
.terminal-frame,
.mobile-keybar { scrollbar-width: none; }
.terminal-frame::-webkit-scrollbar,
.mobile-keybar::-webkit-scrollbar { display: none; }
```

Do not change `overflow: auto`, `overflow-x: auto`, `touch-action`, `overscroll-behavior`, or the viewport-lock/display-mode rules.

- [ ] **Step 2: Run the focused contract test and verify GREEN**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
```

Expected: PASS.

- [ ] **Step 3: Run the relevant component and pointer tests**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- \
  src/components/terminal/MobileKeyBar.test.ts \
  src/components/terminal/TerminalCanvas.test.ts \
  src/composables/usePointerViewport.test.ts \
  src/test/responsive-contract.test.ts
```

Expected: PASS with existing keybar containment, terminal overflow, lock, pan, and pinch contracts unchanged.

- [ ] **Step 4: Run isolated real-browser acceptance and verify GREEN**

Run:

```bash
./scripts/run-web-e2e.sh
```

Expected: desktop, mobile portrait, and mobile landscape all PASS. In both mobile projects, computed scrollbar presentation is `none`; the existing horizontal touch drag still advances keybar `scrollLeft`; page geometry, terminal pan/pinch, and lock behavior remain stable. The disposable fixture removes its temporary directory on success.

- [ ] **Step 5: Inspect the scoped diff**

Run:

```bash
git diff --check
git diff HEAD~1 -- \
  packages/client-ui/src/styles/terminal-responsive.css \
  packages/client-ui/src/test/responsive-contract.test.ts \
  apps/clients/web/e2e/control-center.spec.ts
```

Expected: no whitespace errors; the only production change is the two mobile scrollbar-presentation rules.

- [ ] **Step 6: Commit the implementation**

```bash
git add packages/client-ui/src/styles/terminal-responsive.css
git commit -m "fix(client): hide mobile terminal scrollbars"
```

### Task 3: Full verification, deployment, and cleanup

**Files:**
- Verify only; no planned source changes.

- [ ] **Step 1: Run the complete frontend verification set**

Run:

```bash
npm run test:run
npm run typecheck
npm run build:web
```

Expected: all workspace frontend tests pass, every TypeScript workspace typechecks, and the Web production bundle builds successfully.

- [ ] **Step 2: Confirm branch cleanliness before integration**

Run:

```bash
git status --short --branch
git log -3 --oneline
```

Expected: the feature worktree is clean and its latest commits are the failing-test contract followed by the implementation.

- [ ] **Step 3: Integrate the verified feature branch into `main`**

From the main checkout, fast-forward the task branch only after checking that `main` contains the approved design and plan commits and has no unrelated local edits:

```bash
git merge --ff-only fix/mobile-terminal-scrollbar-visibility
```

Expected: the fast-forward succeeds without a merge commit or conflict.

- [ ] **Step 4: Build and recreate only the Control Plane container**

From the main checkout:

```bash
docker compose -f deploy/compose.yaml build control-plane
docker compose -f deploy/compose.yaml up -d --no-deps --force-recreate control-plane
```

Do not remove the `termflow-data` volume and do not stop, delete, or modify real tmux sessions.

- [ ] **Step 5: Verify the deployment is healthy**

Run:

```bash
docker compose -f deploy/compose.yaml ps control-plane
docker inspect deploy-control-plane-1 --format '{{.Image}} {{.State.Health.Status}} {{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}'
curl --fail --silent http://127.0.0.1:8765/healthz
```

Expected: the container is running and healthy, retains `termflow-data:/app/data`, and `/healthz` returns `{"status":"ok"}`.

- [ ] **Step 6: Run deployed read-only browser smoke**

Load the existing local deployment variables without printing them, then run only the non-mutating deployed smoke:

```bash
set -a
source .env
set +a
TERMFLOW_E2E_BASE_URL=http://127.0.0.1:8765 \
TERMFLOW_E2E_ADMIN_TOKEN="$TERMFLOW_ADMIN_TOKEN" \
npx playwright test \
  --config apps/clients/web/playwright.config.ts \
  apps/clients/web/e2e/deployed-smoke.spec.ts
```

Expected: desktop, mobile portrait, and mobile landscape smoke projects PASS without destructive API calls or terminal input.

- [ ] **Step 7: Remove only the task worktree and branch**

Use the finishing-branch workflow to remove the worktree created for this plan after integration. Preserve unrelated worktrees and user changes. Confirm:

```bash
git status --short --branch
git worktree list
```

Expected: `main` contains the implementation, the task worktree and merged task branch are gone, unrelated worktrees remain, and the main checkout is clean.
