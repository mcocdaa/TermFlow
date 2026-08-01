# TermFlow Web Control Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the independently implemented A+B terminal channel and Web C, ship them in one Control Plane image, prove the full browser-to-real-tmux path, and document a safe local and production deployment.

**Architecture:** Build Web C in a Node stage, copy its immutable assets into the Python Control Plane image, and let FastAPI serve the SPA without changing `/api/v1`, WebSocket, or health behavior. Validate the public contract at three levels: static routing tests, real cross-process terminal tests, and an isolated real-browser workflow. Keep A installed locally and B+C containerized.

**Tech Stack:** Docker multi-stage builds, Docker Compose, Node.js 22, Python 3.12, FastAPI static files, pytest, real tmux, Playwright Chromium, curl, uv, npm.

---

## Execution prerequisites

Complete and integrate these plans first:

1. [`2026-08-01-termflow-full-tmux-ab.md`](./2026-08-01-termflow-full-tmux-ab.md)
2. [`2026-08-01-termflow-web-client.md`](./2026-08-01-termflow-web-client.md)

This delivery plan owns root/deployment/docs/cross-system files. It may make narrow integration fixes in A+B or Web C only when a failing cross-boundary test proves the need, and must record such fixes in the relevant test.

## Task 1: Serve Web C from B without swallowing API errors

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Add: `apps/control-plane/src/termflow_control_plane/web.py`
- Add: `apps/control-plane/tests/test_web_hosting.py`
- Modify: `tests/test_repository_contract.py`

- [ ] Write tests for `/`, hashed asset files, a client route such as `/terms/<uuid>`, `/healthz`, an unknown `/api/v1/...` route, and a missing static asset.
- [ ] Add a configurable static directory whose production default points at the image's built Web C assets. If assets are absent in a source-only development run, keep APIs functional and return a small explicit unavailable response at `/`.
- [ ] Serve `index.html` for known SPA navigation paths and client-side fallback paths. Never return the SPA for `/api`, `/api/v1`, `/healthz`, `/docs`, `/openapi.json`, or WebSocket endpoints; unknown API routes remain structured JSON 404 responses.
- [ ] Set immutable cache headers for hashed assets and `no-cache` for `index.html`. Add baseline `Content-Security-Policy`, `Referrer-Policy`, `X-Content-Type-Options`, and frame-denial headers compatible with same-origin WebSockets.
- [ ] Run `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_web_hosting.py -q`; expected: all routes behave as specified.
- [ ] Commit with `git commit -m "feat(control-plane): host the decoupled web client"`.

## Task 2: Build B and Web C into one reproducible image

**Files:**

- Modify: `deploy/Dockerfile.control-plane`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/env.example`
- Add: `deploy/.dockerignore`
- Modify: `tests/deploy/test_compose_contract.py`

- [ ] Add a Node 22 build stage that runs `npm ci` and `npm run build` in `apps/clients/web`, then copies only `dist/` into the final Python image.
- [ ] Keep the final image free of Node, npm caches, frontend source, development dependencies, and test artifacts. Keep the existing non-root `termflow` user and persistent `/app/data` volume.
- [ ] Configure trusted browser origins, public base URL, browser-session limits/TTL, terminal frame/rate/queue limits, and static directory through documented environment variables with safe single-node defaults.
- [ ] Keep Compose bound to loopback by default. Preserve the existing named SQLite volume during rebuilds and require the existing admin token rather than generating or printing one in logs.
- [ ] Expand the Compose contract test to assert one B worker, no Kafka/NATS dependency, Web C build stage, non-root runtime, healthcheck, persistent data, and no published A port.
- [ ] Run `docker compose -f deploy/compose.yaml config --quiet`; expected: exit 0 with required environment supplied.
- [ ] Run `docker build -f deploy/Dockerfile.control-plane -t termflow-control-plane:web .`; expected: image builds successfully.
- [ ] Inspect the image with `docker run --rm --entrypoint sh termflow-control-plane:web -c 'test -f /app/web/index.html && ! command -v node && ! command -v npm'`; expected: exit 0.
- [ ] Commit with `git commit -m "build: package web c with the control plane"`.

## Task 3: Add a real cross-process terminal integration test

**Files:**

- Add: `tests/e2e/test_full_terminal_control.py`
- Add: `tests/e2e/test_terminal_reconnect.py`
- Modify: `tests/e2e/conftest.py`
- Modify: `tests/e2e/test_no_content_persistence.py`

- [ ] Start a temporary SQLite B server on an ephemeral loopback port, enroll a temporary A configuration, create a private real tmux Term, and connect its Bridge. Never touch the user's normal TermFlow config or tmux socket.
- [ ] Create a browser session, open `WS /api/v1/terms/{id}/terminal`, and prove the first bytes contain a real tmux screen/status redraw rather than only one Pane capture.
- [ ] Send raw terminal bytes that run a unique command and assert its output returns. Send semantic split/new-window/navigation/zoom/copy actions and assert real tmux topology changes.
- [ ] Rename the Term through B, assert the real tmux session name and B list agree, then attach/query by stable session ID.
- [ ] Disconnect B/C while a long-running Pane command continues, reconnect within 30 seconds, and prove buffered output resumes. Add a separate forced-gap case that produces a fresh stream and redraw.
- [ ] Open a second terminal owner and assert the first receives `replaced`; detach both and prove the tmux server/session and Pane process remain alive.
- [ ] Scan SQLite rows and captured application logs for unique input/output/token markers; expected: terminal content and secrets are absent while metadata audit rows exist.
- [ ] Run `uv run --all-packages pytest tests/e2e/test_full_terminal_control.py tests/e2e/test_terminal_reconnect.py tests/e2e/test_no_content_persistence.py -q`; expected: all pass with real tmux.
- [ ] Commit with `git commit -m "test: prove browser to tmux terminal control"`.

## Task 4: Add isolated real-browser acceptance coverage

**Files:**

- Modify: `apps/clients/web/package.json`
- Modify: `apps/clients/web/package-lock.json`
- Add: `apps/clients/web/playwright.config.ts`
- Add: `apps/clients/web/e2e/control-center.spec.ts`
- Add: `apps/clients/web/e2e/terminal-desktop.spec.ts`
- Add: `apps/clients/web/e2e/terminal-mobile.spec.ts`
- Add: `scripts/run-web-e2e.sh`

- [ ] Add Playwright as a development-only dependency and configure Chromium projects for 1440x900 desktop, 390x844 portrait mobile, and 844x390 landscape mobile.
- [ ] Make `scripts/run-web-e2e.sh` create a temporary config/database/tmux socket, choose free loopback ports, start B and A, run the browser tests, and clean up only its recorded PIDs/temporary directory through a trap.
- [ ] Desktop test: log in, verify dashboard metrics and Computer/Term nesting, rename a Computer and Term, open the terminal, type a command, use display options, split Pane, and confirm the terminal WebSocket receives bytes.
- [ ] Assert display mode changes never send a resize/control frame to A. Assert raw command labels come from fixtures/runtime and no Agent brand label is injected.
- [ ] Mobile tests: open/close the action drawer, use sticky Ctrl/Alt/Shift/Esc/Tab/Prefix, pinch/pan/focus Pane, rotate portrait-to-landscape logic, run an action, and confirm close-Pane requires confirmation.
- [ ] Theme test: switch through all three themes and verify computed semantic tokens change while control state and terminal session stay intact.
- [ ] Capture screenshots and traces only on failure under the temporary test output directory; ensure they are gitignored because terminal content may appear in them.
- [ ] Run `bash scripts/run-web-e2e.sh`; expected: all three Chromium projects pass and cleanup leaves no test tmux server/process.
- [ ] Commit with `git commit -m "test(web): cover desktop and mobile control flows"`.

## Task 5: Document operation, security, and public APIs

**Files:**

- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/protocol.md`
- Modify: `docs/security.md`
- Modify: `docs/api-examples.md`
- Add: `docs/web-client.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] Document the product model exactly: Computer = one `termflow login`; Term = one `termflow new`; Window and Pane are real tmux objects. State that Web C is a decoupled client deployed with B for convenience.
- [ ] Document local A prerequisites and commands for Linux, macOS, and WSL; B+C Docker startup; one-time code retrieval from Web C; Term create/list/attach/stop; and remote terminal access.
- [ ] Document the HTTP and WebSocket endpoints with copyable curl/Python examples. Explain that HTTP Pane input remains for automation while full tmux uses the binary terminal WebSocket.
- [ ] Document A-authoritative rows/cols, client-only display scaling/pan/focus, single remote writer, replacement, reconnect grace/buffer, and the fact that B/C disconnection never stops A's tmux session.
- [ ] Document authentication, cookie/Origin requirements, reverse-proxy TLS setup, token rotation/revocation, SQLite backup, audit metadata, terminal-content non-persistence, rate limits, and safe log handling.
- [ ] Document three themes and the extension contract through semantic design tokens for future App/EXE/Linux clients.
- [ ] Run `uv run pytest tests/docs/test_documentation_contract.py -q`; expected: all documentation contracts pass.
- [ ] Commit with `git commit -m "docs: explain web control deployment and usage"`.

## Task 6: Run final verification and refresh the local demo

**Files:**

- Modify only files needed to correct a proven verification failure.

- [ ] Run `uv sync --all-packages --dev`; expected: environment resolves from the committed lock.
- [ ] Run `uv run --all-packages pytest -q`; expected: the full Python suite passes, including real tmux/e2e tests.
- [ ] Run `uv run --all-packages ruff check .`; expected: exit 0.
- [ ] Run `uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src`; expected: exit 0.
- [ ] Run `npm ci && npm run test:run && npm run typecheck && npm run build` in `apps/clients/web`; expected: all pass.
- [ ] Run `bash scripts/run-web-e2e.sh`; expected: desktop, portrait, and landscape browser suites pass.
- [ ] Run `docker compose -f deploy/compose.yaml config --quiet` with required env; expected: exit 0.
- [ ] Rebuild and force-recreate only the TermFlow `control-plane` service on the configured TermFlow port. Do not stop or bind over unrelated services such as the existing service on port 8000.
- [ ] Verify `GET /healthz`, `GET /`, login cookie creation, dashboard API, existing A Bridge online state, full terminal WebSocket, and one real remote command against the rebuilt container.
- [ ] Verify container image identity, health, volume reuse, one B process, no token/content marker in logs, and continued local attach after Web C closes.
- [ ] Run `git status --short --branch`; expected: clean implementation branch with all planned commits.

## Final acceptance matrix

| Acceptance | Evidence |
| --- | --- |
| B hosts a functional but protocol-decoupled Web C | Tasks 1-2 |
| One image contains B runtime and built Web C only | Task 2 |
| Browser controls a real tmux client, not reconstructed Pane text | Tasks 3-4 |
| Desktop and mobile never resize A | Tasks 3-4 |
| Full keys, semantic actions, themes, and responsive UI work | Task 4 |
| A continues through B/C disconnect and Web terminal close | Task 3 |
| No terminal content or secrets persist | Tasks 3, 6 |
| Linux/macOS/WSL and production operation are documented | Task 5 |
| Source, browser, container, and live demo verification all pass | Task 6 |
