# TermFlow Web C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a responsive Web C that authenticates to B, manages Computers and Terms, and renders/controls A's real tmux client on desktop and mobile without changing A's terminal size.

**Architecture:** Implement Web C as an independent Vue SPA under `apps/clients/web`. It talks only to documented `/api/v1` HTTP and WebSocket endpoints, keeps the browser credential in an HttpOnly session cookie, renders terminal bytes with xterm.js, and separates server state from client-only viewport state. Reusable semantic color tokens live in `packages/design-tokens` and support three complete themes.

**Tech Stack:** Node.js 22, Vue 3.5, Vue Router 4, TypeScript 5.7+, Vite 6, Vitest 3, Vue Test Utils 2, xterm.js 6, native CSS, ResizeObserver, Pointer Events, WebSocket.

---

## File ownership

This plan owns only:

- `apps/clients/web/`
- `packages/design-tokens/`

It must not edit Python, protocol, Docker, Compose, or root deployment files. The Web C must not import from `apps/control-plane` or inspect B's SQLite data. The approved behavior is defined in
[`../specs/2026-08-01-termflow-web-control-design.md`](../specs/2026-08-01-termflow-web-control-design.md).

## Task 1: Scaffold the typed Web C application

**Files:**

- Replace: `apps/clients/README.md`
- Add: `apps/clients/web/package.json`
- Add: `apps/clients/web/package-lock.json`
- Add: `apps/clients/web/index.html`
- Add: `apps/clients/web/tsconfig.json`
- Add: `apps/clients/web/tsconfig.app.json`
- Add: `apps/clients/web/vite.config.ts`
- Add: `apps/clients/web/src/env.d.ts`
- Add: `apps/clients/web/src/main.ts`
- Add: `apps/clients/web/src/App.vue`
- Add: `apps/clients/web/src/router.ts`
- Add: `apps/clients/web/src/test/setup.ts`
- Add: `apps/clients/web/src/views/LoginView.vue`
- Add: `apps/clients/web/src/views/DashboardView.vue`
- Add: `apps/clients/web/src/views/ComputersView.vue`
- Add: `apps/clients/web/src/views/TerminalView.vue`
- Add: `apps/clients/web/src/App.test.ts`

- [ ] Pin compatible dependencies: Vue `^3.5`, Vue Router `^4.5`, Vite `^6`, TypeScript `^5.7`, Vitest `^3`, Vue Test Utils `^2.4`, and `@xterm/xterm ^6`. Generate and commit the npm lockfile with Node 22.
- [ ] Configure scripts `dev`, `build`, `typecheck`, `test`, and `test:run`. Configure Vitest with jsdom and deterministic browser API stubs.
- [ ] Write a failing router-shell test that covers `/login`, `/`, `/computers`, and `/terms/:termId`; unknown client routes render an in-app not-found view.
- [ ] Build a minimal semantic app shell with skip link, top title area, desktop left navigation, and mobile navigation. Route metadata marks authenticated pages.
- [ ] Add a route guard that calls the session-status API and redirects unauthenticated users to `/login`; redirect a successful login back to the originally requested route.
- [ ] Run `npm run test:run -- src/App.test.ts && npm run typecheck` from `apps/clients/web`; expected: both pass.
- [ ] Commit with `git commit -m "feat(web): scaffold independent control client"`.

## Task 2: Create semantic design tokens and three themes

**Files:**

- Add: `packages/design-tokens/package.json`
- Add: `packages/design-tokens/src/tokens.css`
- Add: `packages/design-tokens/src/themes/graphite-signal.css`
- Add: `packages/design-tokens/src/themes/cloud-cobalt.css`
- Add: `packages/design-tokens/src/themes/midnight-indigo.css`
- Add: `packages/design-tokens/src/index.ts`
- Add: `packages/design-tokens/src/contract.test.ts`
- Add: `apps/clients/web/src/styles/reset.css`
- Add: `apps/clients/web/src/styles/app.css`
- Add: `apps/clients/web/src/stores/theme.ts`
- Add: `apps/clients/web/src/components/settings/ThemePicker.vue`
- Add: `apps/clients/web/src/components/settings/ThemePicker.test.ts`
- Modify: `apps/clients/web/src/main.ts`

- [ ] Define semantic tokens for page, panel, elevated surface, terminal surface, border, primary/secondary/muted text, accent, accent contrast, focus, online/warning/danger states, shadow, radius, spacing, and monospace/UI font stacks.
- [ ] Implement all three approved themes: `graphite-signal` as default, `cloud-cobalt`, and `midnight-indigo`. Every theme must assign every semantic token.
- [ ] Add a contract test that parses each theme and fails on missing tokens. Add a production-source test that rejects component-level hex/rgb/hsl color literals outside the theme files.
- [ ] Persist only the theme identifier in localStorage. Apply it as `data-theme` before Vue mounts to avoid flash; never store credentials or terminal content.
- [ ] Build an accessible theme picker with visible swatches, text labels, keyboard navigation, and `aria-checked` state.
- [ ] Run `npm run test:run -- ../../packages/design-tokens/src/contract.test.ts src/components/settings/ThemePicker.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add unified three-theme token system"`.

## Task 3: Implement the HTTP API boundary and browser login

**Files:**

- Add: `apps/clients/web/src/api/types.ts`
- Add: `apps/clients/web/src/api/http.ts`
- Add: `apps/clients/web/src/api/session.ts`
- Add: `apps/clients/web/src/api/dashboard.ts`
- Add: `apps/clients/web/src/api/computers.ts`
- Add: `apps/clients/web/src/api/terms.ts`
- Add: `apps/clients/web/src/api/http.test.ts`
- Add: `apps/clients/web/src/stores/session.ts`
- Modify: `apps/clients/web/src/views/LoginView.vue`
- Add: `apps/clients/web/src/views/LoginView.test.ts`

- [ ] Define explicit TypeScript DTOs matching the public B JSON contract. Do not share generated or imported Python models and do not add fields inferred from B internals.
- [ ] Build one fetch wrapper using relative `/api/v1` URLs, `credentials: "same-origin"`, JSON content negotiation, request abort support, and typed structured errors. Never append credentials to a URL.
- [ ] Login accepts the B admin token only in component memory long enough to POST `/api/v1/session`, then clears the input and state. Logout deletes the server session and resets client stores.
- [ ] Add tests proving the token is absent from localStorage/sessionStorage, URL/history, emitted events, console output, and rendered DOM after successful login.
- [ ] Display actionable offline, authentication, validation, and rate-limit errors without displaying raw server exception text.
- [ ] Run `npm run test:run -- src/api/http.test.ts src/views/LoginView.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add cookie session and typed api client"`.

## Task 4: Build the control-center dashboard

**Files:**

- Add: `apps/clients/web/src/composables/useDashboard.ts`
- Add: `apps/clients/web/src/components/dashboard/MetricCard.vue`
- Add: `apps/clients/web/src/components/dashboard/ComputerCard.vue`
- Add: `apps/clients/web/src/components/dashboard/TermRow.vue`
- Add: `apps/clients/web/src/components/dashboard/StatusPill.vue`
- Modify: `apps/clients/web/src/views/DashboardView.vue`
- Add: `apps/clients/web/src/views/DashboardView.test.ts`

- [ ] Write a failing view test using HTTP fixtures for two Computers, several online/offline Terms, multiple Windows/Panes, and a 24-hour interaction count.
- [ ] Render the overview metrics: online Terms, active Panes, 24-hour interactions, and total Computers. Use plain labels and current server values rather than animations or estimated client counts.
- [ ] Render one large Computer card with nested compact Term rows. Each Term row shows its user-controlled name, raw active `pane_current_command`, Window/Pane counts, online state, and last seen time.
- [ ] Make Term rows proper keyboard-activatable links to `/terms/:termId`. Offline Terms remain visible but the terminal-open action is disabled with a reason.
- [ ] Poll the lightweight dashboard endpoint while visible, pause when the document is hidden, and cancel stale requests. Do not open one terminal WebSocket per dashboard row.
- [ ] Assert the production UI contains no hardcoded Codex/Claude detection, labels, icons, or process-state mapping.
- [ ] Run `npm run test:run -- src/views/DashboardView.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): build computer and term dashboard"`.

## Task 5: Build Computer registration and naming management

**Files:**

- Add: `apps/clients/web/src/components/computers/ComputerTable.vue`
- Add: `apps/clients/web/src/components/computers/ComputerNameEditor.vue`
- Add: `apps/clients/web/src/components/computers/EnrollmentDialog.vue`
- Add: `apps/clients/web/src/components/computers/EnrollmentDialog.test.ts`
- Modify: `apps/clients/web/src/views/ComputersView.vue`
- Add: `apps/clients/web/src/views/ComputersView.test.ts`

- [ ] List display name, hostname, OS/platform, TermFlow version, online Term count, registered time, and last seen time for each Computer.
- [ ] Implement optimistic Computer display-name editing with 1-128-character/control-character validation and rollback on a structured API error.
- [ ] The “添加电脑” dialog creates a one-time code only after an explicit click, displays its expiry countdown, and offers copy plus a platform-neutral `termflow login --server URL --code CODE` command.
- [ ] Keep the one-time code in component memory only, clear it on close/expiry/navigation, and mask it from analytics/console. Add a test asserting the code never reaches storage.
- [ ] Explain in the dialog that one `login` represents one Computer and later `termflow new --name NAME` commands create independent Terms on that Computer.
- [ ] Run `npm run test:run -- src/components/computers/EnrollmentDialog.test.ts src/views/ComputersView.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add computer enrollment management"`.

## Task 6: Implement xterm.js terminal transport and lifecycle

**Files:**

- Add: `apps/clients/web/src/terminal/protocol.ts`
- Add: `apps/clients/web/src/terminal/socket.ts`
- Add: `apps/clients/web/src/terminal/terminalAdapter.ts`
- Add: `apps/clients/web/src/terminal/socket.test.ts`
- Add: `apps/clients/web/src/composables/useTerminalSession.ts`
- Add: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Add: `apps/clients/web/src/components/terminal/TerminalCanvas.test.ts`
- Modify: `apps/clients/web/src/views/TerminalView.vue`

- [ ] Test the WebSocket state machine for connecting, `terminal.ready`, binary output, binary input, authoritative `terminal.size`, reconnect attempt, replacement, offline, stream reset, and clean close.
- [ ] Use the same-origin `ws:`/`wss:` URL derived from `window.location`; rely on the HttpOnly cookie. Set `binaryType="arraybuffer"` and reject unknown text control frames safely.
- [ ] Feed B binary frames directly to xterm.js. Send xterm `onData` and binary/paste data as chunked binary frames no larger than 65,536 bytes.
- [ ] Initialize xterm rows/cols from `terminal.ready`; update them only when B sends `terminal.size`. Do not import FitAddon, call a resize endpoint, or send any local viewport dimensions to B.
- [ ] On a new stream after a gap, reset xterm before accepting bytes. On a resumable disconnect, show a non-blocking reconnect overlay without clearing the existing screen until B determines resume or reset.
- [ ] Dispose all xterm handlers, observers, sockets, retry timers, and modifier state on route leave. Terminal scrollback remains memory-only and is not written to browser storage.
- [ ] Run `npm run test:run -- src/terminal/socket.test.ts src/components/terminal/TerminalCanvas.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): stream full tmux sessions through xterm"`.

## Task 7: Add desktop display controls without resizing A

**Files:**

- Add: `apps/clients/web/src/terminal/viewport.ts`
- Add: `apps/clients/web/src/terminal/viewport.test.ts`
- Add: `apps/clients/web/src/components/terminal/TerminalTitlebar.vue`
- Add: `apps/clients/web/src/components/terminal/DisplayMenu.vue`
- Add: `apps/clients/web/src/components/terminal/DisplayMenu.test.ts`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Modify: `apps/clients/web/src/views/TerminalView.vue`

- [ ] Model display mode as a client-only discriminated union: `scale-50`, `scale-75`, `font-100`, and `fit`. Render the four choices vertically with filled/empty radio indicators in one title-bar display button.
- [ ] `50%` and `75%` scale the complete A grid; `100%` uses the configured actual terminal font size; `fit` computes the largest uniform scale that fits the A rows/cols inside the viewport. None may call `terminal.resize()` with client-derived geometry.
- [ ] Place display and expandable tmux action controls in the top title bar at equal height. Menus float over the terminal and do not consume terminal layout rows.
- [ ] Keep the terminal grid aligned to physical pixels where possible and expose horizontal/vertical panning when the selected scale exceeds the viewport.
- [ ] Add unit tests across common desktop widths proving display changes alter only CSS transform/font presentation and do not produce WebSocket frames.
- [ ] Run `npm run test:run -- src/terminal/viewport.test.ts src/components/terminal/DisplayMenu.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add authoritative-size display controls"`.

## Task 8: Add responsive mobile viewport and orientation behavior

**Files:**

- Add: `apps/clients/web/src/composables/usePointerViewport.ts`
- Add: `apps/clients/web/src/composables/usePointerViewport.test.ts`
- Add: `apps/clients/web/src/components/terminal/PaneFocusMenu.vue`
- Add: `apps/clients/web/src/styles/terminal-responsive.css`
- Modify: `apps/clients/web/src/components/terminal/TerminalCanvas.vue`
- Modify: `apps/clients/web/src/views/TerminalView.vue`

- [ ] Implement two-pointer pinch zoom around the gesture midpoint and one-pointer pan only when terminal selection/mouse reporting is not active. Clamp scale and pan so the full terminal cannot be lost permanently off-screen.
- [ ] Use topology geometry (`left`, `top`, `width`, `height`) to calculate a client-only “聚焦 Pane” crop. This action must not send tmux zoom or resize messages.
- [ ] Keep portrait and landscape behavior identical: manual zoom, pan, focus, and a hidden-by-default auxiliary drawer. Orientation changes recompute viewport transforms but preserve the chosen mode where possible.
- [ ] Respect safe-area insets and prevent the mobile action trigger/drawer from covering the terminal input caret after it closes.
- [ ] Add pointer-event tests for pinch, pan, focus Pane, orientation change, and zero emitted terminal control frames.
- [ ] Run `npm run test:run -- src/composables/usePointerViewport.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add mobile pan zoom and pane focus"`.

## Task 9: Add full keyboard input and auxiliary tmux controls

**Files:**

- Add: `apps/clients/web/src/terminal/actions.ts`
- Add: `apps/clients/web/src/terminal/modifiers.ts`
- Add: `apps/clients/web/src/terminal/modifiers.test.ts`
- Add: `apps/clients/web/src/components/terminal/TmuxActionMenu.vue`
- Add: `apps/clients/web/src/components/terminal/MobileKeyBar.vue`
- Add: `apps/clients/web/src/components/terminal/ClosePaneDialog.vue`
- Add: `apps/clients/web/src/components/terminal/TmuxControls.test.ts`
- Modify: `apps/clients/web/src/views/TerminalView.vue`

- [ ] Keep physical desktop keyboard and IME input flowing through xterm.js. Build sticky one-shot/mobile controls for Ctrl, Alt, Shift, Esc, Tab, and the server-reported tmux Prefix.
- [ ] Implement desktop title-bar action menu and mobile click-to-expand/click-to-collapse overlay drawer for split left/right, split top/bottom, new Window, directional Pane navigation, tmux zoom, copy mode, close Pane, and searchable more actions.
- [ ] Show server-reported actual key bindings in hover/focus tooltips. Labels describe semantic actions and never assume `Ctrl+B` when A reports another prefix.
- [ ] Send semantic action JSON frames, not fabricated shortcut bytes. Reset one-shot modifiers after a key, terminal replacement, blur timeout, or route leave.
- [ ] Require a modal confirmation naming the target Pane before sending `close_pane` with `confirmed=true`. Keyboard focus must be trapped and restored.
- [ ] Add tests for mouse, touch, keyboard, alternate prefix display, action result errors, modifier reset, hidden mobile drawer, and destructive confirmation.
- [ ] Run `npm run test:run -- src/terminal/modifiers.test.ts src/components/terminal/TmuxControls.test.ts && npm run typecheck`; expected: all pass.
- [ ] Commit with `git commit -m "feat(web): add tmux keyboard and action controls"`.

## Task 10: Complete Web C quality gates

**Files:**

- Add: `apps/clients/web/src/test/a11y-contract.test.ts`
- Add: `apps/clients/web/src/test/privacy-contract.test.ts`
- Add: `apps/clients/web/src/test/responsive-contract.test.ts`
- Modify: only Web C or design-token files listed in Tasks 1-9 when a quality-gate failure identifies the defect.

- [ ] Test keyboard access, landmark labels, focus visibility, modal focus behavior, status text independent of color, and reduced-motion behavior.
- [ ] Test 360x800 portrait, 800x360 landscape, 1024x768, and 1440x900 layouts for navigation reachability, no title-bar overlap, hidden mobile drawer, and usable terminal viewport.
- [ ] Test that token values and terminal output samples do not enter localStorage, sessionStorage, IndexedDB wrappers, console methods, URL state, or application telemetry objects.
- [ ] Run `npm run test:run`; expected: all Web C and design-token tests pass.
- [ ] Run `npm run typecheck && npm run build`; expected: no TypeScript errors and `dist/` is produced successfully.
- [ ] Run `rg -n "#[0-9a-fA-F]{3,8}|rgb\\(|hsl\\(" src --glob '*.vue' --glob '*.css'`; expected: no component color literals outside imported theme sources.
- [ ] Run `rg -ni "codex|claude" src`; expected: no hardcoded Agent detection or branding.
- [ ] Run `git status --short`; expected: clean after the final commit.

## Requirement coverage

| Requirement | Task |
| --- | --- |
| Web C independent from B internals | 1, 3 |
| Three unified selectable themes | 2 |
| Dashboard and Computer-nested Term list | 4 |
| Computer registration and rename | 5 |
| Real tmux display and full input | 6, 9 |
| C cannot change A rows/cols | 6, 7, 8 |
| Desktop single display menu and title-bar actions | 7, 9 |
| Mobile portrait/landscape pan, zoom, focus, collapsible actions | 8, 9 |
| Raw running command, user names, no Agent hardcoding | 4, 10 |
| Browser token and terminal-content privacy | 3, 6, 10 |
