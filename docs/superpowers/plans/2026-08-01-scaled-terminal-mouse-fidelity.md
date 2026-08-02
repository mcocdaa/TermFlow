# Scaled Terminal Mouse Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tmux mouse reporting, wheel input, and xterm selection behave identically in 50%, 75%, fit, 100%, Pane-focus, and client zoom modes without changing A-side rows or columns.

**Architecture:** Replace CSS scaling of the xterm DOM with xterm's public `fontSize` presentation control. Keep an immutable 100% font/cell baseline, apply every visual scale from that baseline, size the outer viewport from the rendered cell geometry, and leave only translation on the terminal grid. Verify the result against a disposable real tmux instance with mouse reporting enabled.

**Tech Stack:** Vue 3, TypeScript, xterm.js 6, Vitest, Playwright, tmux 3.2+, Python fixture tooling.

---

### Task 1: Add adapter-level visual scale behavior

**Files:**

- Modify: `apps/clients/web/src/terminal/terminalAdapter.ts`
- Create: `apps/clients/web/src/terminal/terminalAdapter.test.ts`

- [ ] **Step 1: Write a failing unit test for stable font scaling**

Create a small exported pure helper contract and test it before changing the adapter implementation:

```ts
import { describe, expect, it } from 'vitest'
import { visualFontSize } from './terminalAdapter'

describe('xterm visual font scaling', () => {
  it('always derives from the 100% base instead of accumulating scale', () => {
    expect(visualFontSize(14, 0.5)).toBe(7)
    expect(visualFontSize(14, 0.75)).toBe(10.5)
    expect(visualFontSize(14, 1)).toBe(14)
  })

  it('normalizes invalid visual scales without changing terminal geometry', () => {
    expect(visualFontSize(14, 0)).toBe(14)
    expect(visualFontSize(14, Number.NaN)).toBe(14)
  })
})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/terminal/terminalAdapter.test.ts
```

Expected: FAIL because `visualFontSize` is not exported.

- [ ] **Step 3: Implement the pure scaling helper and adapter method**

Add a shared metrics type and public operation:

```ts
export interface TerminalCellMetrics { width: number; height: number }

export function visualFontSize(baseFontSize: number, scale: number): number {
  const normalized = Number.isFinite(scale) && scale > 0 ? scale : 1
  return baseFontSize * normalized
}

export interface TerminalAdapter {
  write(bytes: Uint8Array): void
  resize(cols: number, rows: number): void
  reset(): void
  focus(): void
  refreshTheme(): void
  setInputEnabled(enabled: boolean): void
  measureCell(): TerminalCellMetrics | null
  setVisualScale(scale: number): TerminalCellMetrics | null
  canClientPan(): boolean
  dispose(): void
}
```

Inside `createXtermAdapter`, retain `const baseFontSize = 14`, extract the existing screen measurement into one closure, and implement:

```ts
setVisualScale: (scale) => {
  terminal.options.fontSize = visualFontSize(baseFontSize, scale)
  return measureCell()
},
```

Do not call `terminal.resize` from this method.

- [ ] **Step 4: Run the adapter test and Web typecheck**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/terminal/terminalAdapter.test.ts
npm run typecheck
```

Expected: tests pass; typecheck initially identifies every fake adapter that must implement `setVisualScale`, then passes after those fakes receive a `vi.fn(() => metrics)` implementation.

- [ ] **Step 5: Commit the adapter contract**

```bash
git add apps/clients/web/src/terminal/terminalAdapter.ts apps/clients/web/src/terminal/terminalAdapter.test.ts apps/clients/web/src/components/terminal/TerminalCanvas.test.ts
git commit -m "refactor(web): add native xterm visual scaling"
```

### Task 2: Drive TerminalCanvas presentation through xterm geometry

**Files:**

- Modify: `apps/clients/web/src/composables/useTerminalSession.ts`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.test.ts`
- Modify: `apps/clients/web/src/terminal/viewport.test.ts`
- Modify: `apps/clients/web/src/styles/app.css`
- Modify: `apps/clients/web/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing component assertions for 50% and fit**

Extend the fake adapter with `setVisualScale`. Mount TerminalCanvas with `displayMode: 'scale-50'`, send `terminal.ready`, and assert:

```ts
expect(adapter.setVisualScale).toHaveBeenLastCalledWith(0.5)
expect(wrapper.get('.terminal-grid').attributes('style')).not.toContain('scale(')
expect(wrapper.get('.terminal-grid').attributes('style')).toContain('translate(')
expect(socket.sendInput).not.toHaveBeenCalled()
```

Update props to `fit`, set a deterministic frame geometry and base cell size, then assert the adapter receives the calculated fit scale while no socket input or resize message is created. Add a repeated mode-change assertion proving the requested scale is absolute, not cumulative.

Update the responsive CSS contract to reject a transform containing `scale(` on `.terminal-grid` while retaining fit overflow hiding and `transform-origin: 0 0` only if translation still needs it.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/components/terminal/TerminalCanvas.test.ts src/terminal/viewport.test.ts src/test/responsive-contract.test.ts
```

Expected: FAIL because TerminalCanvas still emits a CSS scale transform and does not call the adapter visual-scale method.

- [ ] **Step 3: Expose visual scale through the session composable**

Add this method to `useTerminalSession`'s returned API:

```ts
setVisualScale: (scale: number) => adapter?.setVisualScale(scale) ?? null,
```

It must not call the socket.

- [ ] **Step 4: Separate baseline and rendered metrics in TerminalCanvas**

Replace the single cell metric ref with:

```ts
const baselineCellMetrics = ref<TerminalCellMetrics | null>(null)
const renderedCellMetrics = ref<TerminalCellMetrics | null>(null)
const appliedVisualScale = ref(1)
```

Use baseline metrics in `displayPresentation`. On the first valid measurement, set both baseline and rendered metrics. Watch the requested total scale and authoritative dimensions, call `session.setVisualScale(requestedScale)`, and store its returned rendered metrics.

For fit with no additional pointer zoom, compare `cols * rendered.width` and `rows * rendered.height` to the frame. If either exceeds the frame by more than one pixel, calculate one downward-only correction:

```ts
const correction = Math.min(
  1,
  frame.value.width / renderedWidth,
  frame.value.height / renderedHeight,
)
const correctedScale = requestedScale * correction * 0.999
```

Apply that correction once for the stable frame/dimensions/display tuple. Never increase scale and never recurse through rendered metrics.

- [ ] **Step 5: Remove CSS geometry scaling**

Build actual grid dimensions from `renderedCellMetrics * rows/cols`. Change grid style to:

```ts
{
  width: `${renderedGridWidth}px`,
  height: `${renderedGridHeight}px`,
  transform: `translate(${pointer.state.panX}px, ${pointer.state.panY}px)`,
}
```

Keep outer overflow behavior and fit-mode overflow hiding. Pointer handlers must continue returning immediately for `pointerType === 'mouse'`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
cd apps/clients/web
npm run test:run -- src/components/terminal/TerminalCanvas.test.ts src/terminal/viewport.test.ts src/composables/usePointerViewport.test.ts src/test/responsive-contract.test.ts
npm run typecheck
```

Expected: all focused tests and typecheck pass.

- [ ] **Step 7: Commit canvas presentation changes**

```bash
git add apps/clients/web/src/composables/useTerminalSession.ts apps/clients/web/src/components/terminal/TerminalCanvas.vue apps/clients/web/src/components/terminal/TerminalCanvas.test.ts apps/clients/web/src/terminal/viewport.test.ts apps/clients/web/src/styles/app.css apps/clients/web/src/test/responsive-contract.test.ts
git commit -m "fix(web): preserve mouse geometry in scaled terminals"
```

### Task 3: Add disposable real-tmux mouse acceptance

**Files:**

- Modify: `scripts/web_e2e_fixture.py`
- Modify: `apps/clients/web/e2e/control-center.spec.ts`

- [ ] **Step 1: Extend the disposable fixture with a large grid and mouse mode**

Start only the fixture's pexpect client with a deterministic large terminal:

```py
child = pexpect.spawn(
    str(repo / ".venv/bin/termflow"),
    ["new", "--name", "resume-terminal"],
    timeout=8,
    encoding=None,
    dimensions=(60, 200),
)
```

After detach, enable mouse on only that private socket through `TmuxRunner(instance.socket_path).run_command("set-option", "-g", "mouse", "on")`. The fixture's XDG directories and socket are already disposable.

- [ ] **Step 2: Write browser assertions that fail with CSS scaling**

In the desktop project, after creating left/right Panes:

1. Select the left Pane through a known keyboard/action path.
2. In 50% mode, click the visual center of the right Pane calculated from topology geometry and the `.xterm-screen` bounding box.
3. Poll the topology endpoint until the right Pane is active.
4. Repeat after switching to fit.
5. Capture WebSocket frames, wheel over a known terminal cell, decode the SGR mouse report, and assert its row/column matches the visual target while `.terminal-frame.scrollTop` does not change in fit.
6. Turn tmux mouse off from the disposable terminal, clear the screen to two distinct words, double-click the second word in both 50% and fit, copy it, and assert the clipboard contains that second word rather than the first.

The current CSS-transform implementation must fail on the right-Pane coordinate and second-word selection assertions.

- [ ] **Step 3: Run desktop E2E before Task 2 production changes and record RED**

Execute Task 3 Steps 1-3 immediately after Task 1 and before Task 2 Step 3 changes production code. Run:

```bash
./scripts/run-web-e2e.sh
```

Expected before Task 2: desktop mouse fidelity assertion fails; the disposable runner prints an evidence directory and cleans exact runtime resources on a successful rerun.

- [ ] **Step 4: Run disposable browser E2E against the fix**

Run:

```bash
./scripts/run-web-e2e.sh
```

Expected: desktop, mobile portrait, and mobile landscape projects pass; screenshots show no fit overflow; exact temporary Term, server process, and run directory are removed.

- [ ] **Step 5: Commit acceptance coverage**

```bash
git add scripts/web_e2e_fixture.py apps/clients/web/e2e/control-center.spec.ts
git commit -m "test(web): cover scaled tmux mouse fidelity"
```

### Task 4: Run full quality gates and integrate

**Files:**

- Verify only; modify only files already listed if a gate exposes a regression.

- [ ] **Step 1: Run all Web tests, typecheck, and production build**

```bash
cd apps/clients/web
npm run test:run
npm run typecheck
npm run build
```

Expected: all tests pass, TypeScript reports no errors, and Vite produces `dist/`.

- [ ] **Step 2: Run repository verification**

```bash
cd /home/mcocdaa/AI_CODE/TermFlow/.worktrees/scaled-terminal-mouse-fidelity
./scripts/verify.sh
```

Expected: Python protocol, Node, Control Plane, cross-process, docs, deployment, Ruff, mypy, and Web gates pass with no failures.

- [ ] **Step 3: Review the exact diff and privacy boundary**

```bash
git diff main...HEAD --check
git diff --stat main...HEAD
rg -n "terminal\.resize|FitAddon|scale\(" apps/clients/web/src
```

Expected: no whitespace errors; no client resize path or FitAddon; any remaining `scale(` belongs to non-terminal UI animation or pure test data, not an ancestor of `.xterm-screen`.

- [ ] **Step 4: Merge locally under the user's standing authorization**

Fast-forward/pull main if available, merge the feature branch locally, rerun the full verification command on merged main, then remove only the worktree created for this plan and delete its merged feature branch.

- [ ] **Step 5: Rebuild and restart only B's Control Plane container**

Use the repository Compose configuration to rebuild the image containing the Web client and force-recreate the Control Plane service. Do not restart or kill existing A-side tmux Sessions or Bridge processes.

- [ ] **Step 6: Verify deployment and live A preservation**

```bash
curl -fsS http://127.0.0.1:8765/healthz
termflow list --json
```

Expected: B is healthy; all previously running Terms retain the same instance IDs, live tmux Sessions, and live Bridge processes. Report any deployment gap instead of claiming live completion.
