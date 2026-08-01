# TermFlow Web Usability Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make enrollment, Computer metadata, dashboard navigation, and the terminal viewport match the approved product semantics and remove the terminal page's double-scroll behavior.

**Architecture:** Keep B as the UTC authority and atomic enrollment issuer, while Web C only formats metadata and controls client-side presentation. Replace fixed terminal height arithmetic with a two-row grid, make fit mode overflow-free without resizing A, and use a licensed Vue SVG icon package for interactive affordances.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic Settings, Vue 3, TypeScript, Vitest, Playwright, xterm.js, Lucide Vue Next, Docker.

---

## File map

- `apps/control-plane/src/termflow_control_plane/config.py`: authoritative enrollment TTL setting.
- `apps/control-plane/src/termflow_control_plane/api/enrollment.py`: HTTP enrollment issuance.
- `apps/control-plane/src/termflow_control_plane/cli.py`: CLI enrollment issuance using the same TTL.
- `apps/clients/web/src/components/ui/AppIcon.vue`: one consistent Lucide sizing/accessibility boundary if repeated icon props require it; otherwise import individual tree-shaken icons directly.
- `apps/clients/web/src/components/computers/EnrollmentDialog.vue`: 60-second countdown, automatic replacement, headings, help and copy behavior.
- `apps/clients/web/src/components/computers/{ComputerTable,ComputerNameEditor}.vue`: metadata semantics, timezone formatting and name-as-editor trigger.
- `apps/clients/web/src/components/dashboard/{ComputerCard,TermRow}.vue`: safe metadata joining and whole-row navigation.
- `apps/clients/web/src/components/terminal/{TerminalTitlebar,DisplayMenu,TmuxActionMenu,PaneFocusMenu,TerminalCanvas}.vue`: click-only controls, open-state icons and viewport mode behavior.
- `apps/clients/web/src/styles/{app,reset,terminal-responsive}.css`: interaction states and overflow-safe terminal layout.
- `apps/clients/web/e2e/control-center.spec.ts`: real-browser no-overflow and click-navigation acceptance.
- `docs/{security,web-client}.md`: current 60-second and UTC/local-time behavior.

### Task 1: Make B issue 60-second single-use enrollment codes everywhere

**Files:**
- Modify: `apps/control-plane/tests/test_config.py`
- Modify: `apps/control-plane/tests/test_enrollment_api.py`
- Modify: `apps/control-plane/tests/test_cli.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/enrollment.py`
- Modify: `apps/control-plane/src/termflow_control_plane/cli.py`

- [ ] **Step 1: Write failing default and expiry tests**

Add assertions that `Settings(...).enrollment_token_ttl_seconds == 60`, that an HTTP-issued
`expires_at` is 59–60 seconds after a server-observed UTC time, and that `_issue_enrollment`
persists the configured TTL. Keep the existing replay assertion proving single use.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --package termflow-control-plane pytest apps/control-plane/tests/test_config.py apps/control-plane/tests/test_enrollment_api.py apps/control-plane/tests/test_cli.py -q
```

Expected: failures show the missing setting and the current ten-minute expiry.

- [ ] **Step 3: Implement the shared setting**

Add this settings field:

```python
enrollment_token_ttl_seconds: int = Field(default=60, ge=10, le=600)
```

Inject `Settings` into the HTTP endpoint and calculate both HTTP and CLI expiry using:

```python
datetime.now(UTC) + timedelta(seconds=settings.enrollment_token_ttl_seconds)
```

Update the CLI docstring to describe a configurable short-lived single-use code.

- [ ] **Step 4: Run tests to verify GREEN**

Run the Step 2 command and expect all selected tests to pass.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): expire enrollment codes after sixty seconds"
```

### Task 2: Clarify the enrollment dialog and automatically replace expired codes

**Files:**
- Modify: `apps/clients/web/package.json`
- Modify: `apps/clients/web/package-lock.json`
- Modify: `apps/clients/web/src/components/computers/EnrollmentDialog.test.ts`
- Modify: `apps/clients/web/src/components/computers/EnrollmentDialog.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Install the licensed icon dependency**

Run from `apps/clients/web`:

```bash
npm install lucide-vue-next
```

Confirm the lockfile records the installed version and its transitive graph.

- [ ] **Step 2: Write failing dialog tests**

Test that the redundant login sentence is absent; headings are `注册码` and `终端执行命令`;
the help control exposes the terminal-copy explanation; the copy button reads `复制命令`; and,
with fake timers, expiry triggers exactly one replacement request whose new token and command
replace the old values without entering local or session storage.

- [ ] **Step 3: Run tests to verify RED**

```bash
npm test -- EnrollmentDialog.test.ts --run
```

Expected: label, help and automatic-refresh assertions fail against the current component.

- [ ] **Step 4: Implement a race-safe refresh loop**

Keep one interval for countdown display. When `secondsRemaining` reaches zero, clear only the
expired secret and call a guarded `create(true)` that refuses concurrent requests and stops if the
dialog has closed. Render Lucide `X`, `CircleHelp`, `Copy` and `RefreshCw` icons with accessible
button labels. The help popover must appear on both `:hover` and `:focus-visible`.

- [ ] **Step 5: Run tests to verify GREEN**

Run the Step 3 command and expect the dialog suite to pass without timer leaks.

- [ ] **Step 6: Commit**

```bash
git add apps/clients/web/package.json apps/clients/web/package-lock.json apps/clients/web/src/components/computers apps/clients/web/src/styles/app.css
git commit -m "feat(web): refine automatic computer enrollment"
```

### Task 3: Correct Computer metadata, editing and Term navigation

**Files:**
- Modify: `apps/clients/web/src/views/ComputersView.test.ts`
- Modify: `apps/clients/web/src/views/DashboardView.test.ts`
- Modify: `apps/clients/web/src/components/computers/ComputerTable.vue`
- Modify: `apps/clients/web/src/components/computers/ComputerNameEditor.vue`
- Modify: `apps/clients/web/src/components/dashboard/ComputerCard.vue`
- Modify: `apps/clients/web/src/components/dashboard/TermRow.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write failing metadata and interaction tests**

Cover these behaviors independently: absent hostname/platform/version produces no `未报告 hostname`,
`TermFlow null`, or isolated `·`; the column says `操作系统`; timezone-formatted times include a
zone; clicking the name enters edit mode without an edit button; online Term rows are links whose
accessible name includes the real Term name; no `打开终端` button exists; offline rows are not links.

- [ ] **Step 2: Run tests to verify RED**

```bash
npm test -- ComputersView.test.ts DashboardView.test.ts --run
```

Expected: the current fallback copy, edit button and nested open link violate the assertions.

- [ ] **Step 3: Implement semantic rendering**

Build metadata arrays with `filter(Boolean).join(' · ')`, omit empty metadata rows, and format B UTC
timestamps via `Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short',
timeZoneName: 'short' })`. Change the name display to a button styled as text. Render an online
`TermRow` as one `RouterLink` and an offline row as a noninteractive article.

- [ ] **Step 4: Add theme-consistent hover, focus and active states**

Use color-mix values derived from existing tokens; do not hard-code theme-specific colors. Apply
visible `:hover`, `:focus-visible` and `:active` states to buttons, name triggers and Term rows while
respecting `prefers-reduced-motion`.

- [ ] **Step 5: Run tests to verify GREEN**

Run the Step 2 command and expect all selected tests to pass.

- [ ] **Step 6: Commit**

```bash
git add apps/clients/web/src/views apps/clients/web/src/components/computers apps/clients/web/src/components/dashboard apps/clients/web/src/styles/app.css
git commit -m "feat(web): make computers and terms directly interactive"
```

### Task 4: Remove terminal page overflow and make all menus click-only

**Files:**
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TmuxControls.test.ts`
- Modify: `apps/clients/web/src/views/TerminalView.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`
- Modify: `apps/clients/web/src/components/terminal/DisplayMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/PaneFocusMenu.vue`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Modify: `apps/clients/web/src/views/TerminalView.vue`
- Modify: `apps/clients/web/src/styles/app.css`
- Modify: `apps/clients/web/src/styles/terminal-responsive.css`

- [ ] **Step 1: Write failing menu and fit-mode tests**

Assert Lucide SVGs replace literal arrows/circles/chevrons, menu triggers carry an open class or
`aria-expanded=true`, Tmux hover alone cannot open its menu, and selecting fit resets the exposed
viewport. Add structural assertions for the terminal Grid layout and fit-mode overflow contract.

- [ ] **Step 2: Run tests to verify RED**

```bash
npm test -- DisplayMenu.test.ts TmuxControls.test.ts TerminalView.test.ts TerminalCanvas.test.ts --run
```

Expected: literal-symbol and hover-preview behavior fail the new contract.

- [ ] **Step 3: Implement click-only icon controls**

Use Lucide `ArrowLeft`, `ChevronDown`, `Circle`, `CircleDot`, `MonitorCog`, `Command`, and `Focus`
components. Remove `preview`, `mouseenter`, and `mouseleave` from `TmuxActionMenu`; derive menu
visibility only from click state. Style `[aria-expanded='true']` and rotate the Chevron.

- [ ] **Step 4: Implement the root layout fix**

Make `.terminal-view` a two-row Grid; set terminal-route shell/main/page overflow to hidden; set
`.terminal-frame` to `min-height: 0` and mode-specific overflow; remove `.terminal-host` padding;
hide `.xterm-viewport` overflow only in fit mode. When setting fit mode, call `resetViewport()` after
Vue updates so the selected mode means the full remote grid.

- [ ] **Step 5: Run tests to verify GREEN**

Run the Step 2 command and expect all selected tests to pass.

- [ ] **Step 6: Commit**

```bash
git add apps/clients/web/src/components/terminal apps/clients/web/src/views/TerminalView* apps/clients/web/src/styles
git commit -m "fix(web): make terminal fit mode overflow-free"
```

### Task 5: Prove behavior in an isolated real browser and update documentation

**Files:**
- Modify: `apps/clients/web/e2e/control-center.spec.ts`
- Modify: `docs/security.md`
- Modify: `docs/web-client.md`

- [ ] **Step 1: Extend Playwright acceptance before the final CSS change is trusted**

After selecting fit, evaluate `document.scrollingElement`, `.terminal-view`, `.terminal-frame`, and
`.xterm-viewport`; assert the document and frame have no vertical overflow. Hover each closed menu
trigger and assert its menu stays hidden, then click and assert it opens. On the dashboard click the
Term row itself, not an `打开终端` child.

- [ ] **Step 2: Run the isolated browser suite**

Use the repository's `scripts/run-web-e2e.sh` with a unique artifact directory. It must create a
fresh image, container, database and port, then remove only those exact resources. Expect all
desktop, mobile portrait and mobile landscape projects to pass.

- [ ] **Step 3: Inspect viewport screenshots**

Open the desktop and both mobile images with the local image viewer. Confirm left-aligned title,
single click feedback, no duplicate right scrollbar, readable menu layers, and no clipped mobile
actions.

- [ ] **Step 4: Update current behavior docs**

Change ten-minute references to 60 seconds and document B-recorded UTC plus C-local display. State
that closing Web C does not revoke a copied code.

- [ ] **Step 5: Run full verification**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy apps packages
npm --prefix apps/clients/web test -- --run
npm --prefix apps/clients/web run typecheck
npm --prefix apps/clients/web run build
```

Expected: zero failures and exit code 0 for every command.

- [ ] **Step 6: Commit**

```bash
git add apps/clients/web/e2e/control-center.spec.ts docs/security.md docs/web-client.md
git commit -m "test(web): verify polished control workflows"
```

### Task 6: Deploy B without touching A's tmux state

**Files:**
- Verify only: `deploy/docker-compose.yml`
- Verify only: running TermFlow containers and volumes

- [ ] **Step 1: Record the current B container, image, volume and online Term IDs**

Use read-only Docker inspect and authenticated API calls without printing secrets.

- [ ] **Step 2: Build a new uniquely identified control-plane image**

Build from the verified commit and capture the resulting image digest.

- [ ] **Step 3: Replace only the B container while reusing its persistent volume**

Keep the previous B container stopped under an exact rollback name. Do not stop A processes or the
MeetFlow service.

- [ ] **Step 4: Verify live recovery**

Assert Docker health, `/healthz`, `/`, browser login, enrollment `expires_at` near 60 seconds,
dashboard Computer/Term metadata, both existing A Bridges online again, terminal WebSocket open,
fit-mode browser assertions, and no new error logs.

- [ ] **Step 5: Final commit/status check**

Confirm `git status --short` is empty and report the commit, image digest, running URL, test counts,
registration lifecycle, UTC/local-time rule and any remaining stopped rollback container.
