# TermFlow Shared Client Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the current, tested Web C into generated contracts, a DOM-free client core, and a reusable Vue UI package while retaining a thin browser entry and preserving all current behavior.

**Architecture:** The Python protocol remains the public contract source. `client-contracts` contains deterministic generated TypeScript DTOs and terminal-frame codecs, `client-core` owns transport-independent HTTP/session/terminal state, and `client-ui` owns Vue/xterm presentation. `apps/clients/web` becomes a browser-only composition root; this plan deliberately does not add TOTP, native OAuth, Tauri, or Docker changes.

**Tech Stack:** Python 3.12, Pydantic 2, Node 22, npm workspaces, TypeScript 5.7, Vue 3.5, Vitest 3, xterm.js 6, Playwright.

---

## Locked file ownership

```text
packages/protocol/                 Python source of HTTP and WS contracts
packages/client-contracts/         generated TS DTOs plus safe runtime frame codecs
packages/client-core/              no Vue, DOM, window, WebSocket, localStorage or Tauri imports
packages/client-ui/                Vue pages/components, xterm adapter, responsive CSS and themes
apps/clients/web/                  browser transports, browser history, storage and mount only
packages/design-tokens/            unchanged canonical theme tokens
```

The dependency direction is fixed:

```text
protocol -> generated client-contracts -> client-core -> client-ui -> web
design-tokens -------------------------------------------> client-ui
```

The current Web C baseline that every task must preserve is:

```text
24 Vitest files / 86 tests
production Web build succeeds under Node 22.23.2
259 Python tests pass when run outside the restricted thread sandbox
```

### Task 1: Root npm workspace and package boundaries

**Files:**

- Create: `.nvmrc`
- Create: `package.json`
- Create: `package-lock.json`
- Create: `tsconfig.base.json`
- Create: `tests/test_client_workspace_contract.py`
- Create: `packages/client-contracts/package.json`
- Create: `packages/client-contracts/tsconfig.json`
- Create: `packages/client-core/package.json`
- Create: `packages/client-core/tsconfig.json`
- Create: `packages/client-ui/package.json`
- Create: `packages/client-ui/tsconfig.json`
- Modify: `apps/clients/web/package.json`
- Modify: `apps/clients/web/tsconfig.app.json`
- Modify: `packages/design-tokens/package.json`
- Delete: `apps/clients/web/package-lock.json`

- [ ] **Step 1: Write the failing workspace contract test**

```python
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _manifest(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text())


def test_client_workspace_has_one_lock_and_fixed_dependency_direction() -> None:
    root = _manifest("package.json")
    assert root["engines"] == {"node": ">=22 <23"}
    assert root["packageManager"] == "npm@10.9.8"
    assert set(root["workspaces"]) == {
        "apps/clients/*",
        "packages/design-tokens",
        "packages/client-contracts",
        "packages/client-core",
        "packages/client-ui",
    }
    assert (ROOT / "package-lock.json").is_file()
    assert not (ROOT / "apps/clients/web/package-lock.json").exists()

    contracts = _manifest("packages/client-contracts/package.json")
    core = _manifest("packages/client-core/package.json")
    ui = _manifest("packages/client-ui/package.json")
    web = _manifest("apps/clients/web/package.json")
    assert contracts.get("dependencies", {}) == {}
    assert core["dependencies"] == {"@termflow/client-contracts": "0.1.0"}
    assert set(ui["dependencies"]) >= {
        "@termflow/client-core",
        "@termflow/design-tokens",
        "vue",
        "vue-router",
    }
    assert set(web["dependencies"]) >= {"@termflow/client-core", "@termflow/client-ui"}
```

- [ ] **Step 2: Run the test and confirm the root manifest is missing**

Run: `.venv/bin/python -m pytest tests/test_client_workspace_contract.py -q`

Expected: FAIL because `package.json` does not exist.

- [ ] **Step 3: Create the root workspace manifest and Node pin**

```json
{
  "name": "@termflow/workspace",
  "version": "0.1.0",
  "private": true,
  "packageManager": "npm@10.9.8",
  "engines": { "node": ">=22 <23" },
  "workspaces": [
    "apps/clients/*",
    "packages/design-tokens",
    "packages/client-contracts",
    "packages/client-core",
    "packages/client-ui"
  ],
  "scripts": {
    "contracts:generate": "python scripts/generate-client-contracts/generate.py",
    "contracts:check": "python scripts/generate-client-contracts/generate.py --check",
    "test:run": "npm run test:run --workspaces --if-present",
    "typecheck": "npm run typecheck --workspaces --if-present",
    "build:web": "npm run build --workspace @termflow/web-client"
  }
}
```

Write `22` to `.nvmrc`. Add a root `tsconfig.base.json` with `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `ES2022`, `Bundler` module resolution and `skipLibCheck`.

- [ ] **Step 4: Create the three package manifests and update Web dependencies**

Use version `0.1.0` for every internal package. `client-contracts` has no runtime dependency. `client-core` depends only on `@termflow/client-contracts`. `client-ui` depends on core, design tokens, Vue, Vue Router, Lucide and xterm. Web depends on core and UI while retaining its current direct dependencies until the final migration task removes them.

Each new package must expose `./src/index.ts` during workspace development and provide `test:run` and `typecheck` scripts. Use Vitest with a Node environment for contracts/core and jsdom for UI.

- [ ] **Step 5: Generate the single root lock file**

Run:

```bash
source /home/mcocdaa/.nvm/nvm.sh
nvm exec 22 npm install --package-lock-only
nvm exec 22 npm ci
```

Expected: one root `package-lock.json`; no nested Web lock; all workspaces linked under root `node_modules`.

- [ ] **Step 6: Run the workspace contract and current Web regression**

Run:

```bash
.venv/bin/python -m pytest tests/test_client_workspace_contract.py -q
source /home/mcocdaa/.nvm/nvm.sh
nvm exec 22 npm run test:run --workspace @termflow/web-client
nvm exec 22 npm run build:web
```

Expected: workspace contract PASS, current 86 Web tests PASS, Web build PASS.

- [ ] **Step 7: Commit**

```bash
git add .nvmrc package.json package-lock.json tsconfig.base.json tests/test_client_workspace_contract.py packages/client-contracts packages/client-core packages/client-ui packages/design-tokens/package.json apps/clients/web/package.json apps/clients/web/tsconfig.app.json apps/clients/web/package-lock.json
git commit -m "build(client): establish npm workspace boundaries"
```

### Task 2: Deterministic client contract generation

**Files:**

- Create: `scripts/generate-client-contracts/generate.py`
- Create: `scripts/generate-client-contracts/README.md`
- Create: `packages/client-contracts/src/generated.ts`
- Create: `packages/client-contracts/src/terminal.ts`
- Create: `packages/client-contracts/src/terminal.test.ts`
- Create: `packages/client-contracts/src/index.ts`
- Create: `tests/contracts/test_client_contract_generation.py`
- Modify: `packages/client-contracts/package.json`
- Modify: `apps/clients/web/src/api/types.ts`

- [ ] **Step 1: Write failing Python drift tests**

The test must run the generator into `tmp_path`, compare it byte-for-byte with `packages/client-contracts/src/generated.ts`, and assert:

```python
assert "export interface BrowserSessionResponse" in generated
assert "expires_at: string" in generated
assert "expires_at?: string" not in generated
assert "export type TerminalAction =" in generated
assert "gap?:" not in generated
```

It must also run `python scripts/generate-client-contracts/generate.py --check` and expect exit code zero.

- [ ] **Step 2: Run the drift test and confirm the generator is missing**

Run: `.venv/bin/python -m pytest tests/contracts/test_client_contract_generation.py -q`

Expected: FAIL because the generator does not exist.

- [ ] **Step 3: Implement a deterministic Python annotation renderer**

`generate.py` must import an explicit ordered tuple of public Pydantic models from `termflow_protocol`, render model fields from `model_fields`, and support exactly these annotation shapes:

```python
UUID | datetime          -> string
str | int | float | bool -> string | number | boolean
Literal[...]             -> string/number literal union
Enum                     -> member value union
BaseModel                -> exported interface name
list[T]                  -> T[]
T | None                 -> T | null
```

Unknown annotation shapes must raise `TypeError`; they may not silently become `any` or `unknown`. Output ordering is fixed by the explicit model tuple and field definition order. `--output PATH` writes to another path; `--check` renders in memory and exits non-zero when the checked-in file differs.

Include the current dashboard, computer, enrollment, topology, session and terminal-control models. Do not generate request models containing `SecretStr` into the browser package.

- [ ] **Step 4: Add safe terminal control parsing in contracts**

Move the existing closed `TerminalControl` union into `packages/client-contracts/src/terminal.ts`. Keep `parseTerminalControl(text): TerminalControl | null`, require positive rows/cols, validate every required identity field, reject unknown frame types, and remove the non-server `gap` property.

Move and extend the parser tests so malformed JSON, missing IDs, invalid sizes and unknown frames return `null`, while every server frame model parses.
Change `apps/clients/web/src/api/types.ts` into temporary type-only aliases/re-exports from
`@termflow/client-contracts`; remove it only after every Web caller has migrated.

- [ ] **Step 5: Generate, test and typecheck contracts**

Run:

```bash
.venv/bin/python scripts/generate-client-contracts/generate.py
.venv/bin/python -m pytest tests/contracts/test_client_contract_generation.py packages/protocol/tests/test_http_models.py -q
source /home/mcocdaa/.nvm/nvm.sh
nvm exec 22 npm run test:run --workspace @termflow/client-contracts
nvm exec 22 npm run typecheck --workspace @termflow/client-contracts
```

Expected: all commands PASS and a second generator run leaves no diff.

- [ ] **Step 6: Commit**

```bash
git add scripts/generate-client-contracts packages/client-contracts packages/protocol tests/contracts
git commit -m "build(client): generate TypeScript protocol contracts"
```

### Task 3: DOM-free HTTP client core and browser HTTP adapter

**Files:**

- Create: `packages/client-core/src/http/types.ts`
- Create: `packages/client-core/src/http/apiError.ts`
- Create: `packages/client-core/src/http/apiClient.ts`
- Create: `packages/client-core/src/http/apiClient.test.ts`
- Create: `packages/client-core/src/api/session.ts`
- Create: `packages/client-core/src/api/dashboard.ts`
- Create: `packages/client-core/src/api/computers.ts`
- Create: `packages/client-core/src/api/terms.ts`
- Create: `packages/client-core/src/index.ts`
- Create: `apps/clients/web/src/adapters/browserHttpTransport.ts`
- Create: `apps/clients/web/src/adapters/browserHttpTransport.test.ts`
- Modify: `apps/clients/web/src/api/http.ts`
- Modify: `apps/clients/web/src/api/session.ts`
- Modify: `apps/clients/web/src/api/dashboard.ts`
- Modify: `apps/clients/web/src/api/computers.ts`
- Modify: `apps/clients/web/src/api/terms.ts`
- Modify: `apps/clients/web/src/api/http.test.ts`

- [ ] **Step 1: Write failing core API client tests**

Define the wished-for port and client usage in the test:

```ts
const transport: HttpTransport = {
  request: vi.fn().mockResolvedValue({
    status: 200,
    headers: { get: (name) => name.toLowerCase() === 'content-type' ? 'application/json' : null },
    body: { authenticated: true, expires_at: '2026-08-01T00:00:00Z' },
  }),
}
const api = createApiClient(transport)
await expect(api.sessions.status()).resolves.toMatchObject({ authenticated: true })
expect(transport.request).toHaveBeenCalledWith('/api/v1/admin/session', expect.any(Object))
```

Cover JSON success, empty success, structured `ErrorEnvelope`, malformed error response, abort propagation and exact API paths.

- [ ] **Step 2: Run core tests and confirm exports are missing**

Run: `nvm exec 22 npm run test:run --workspace @termflow/client-core -- src/http/apiClient.test.ts`

Expected: FAIL because `createApiClient` and `HttpTransport` do not exist.

- [ ] **Step 3: Implement the transport-neutral API client**

Use these platform-neutral types:

```ts
export interface HttpRequest {
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
}

export interface HeaderReader {
  get(name: string): string | null
}

export interface HttpResponse {
  status: number
  headers: HeaderReader
  body: unknown
}

export interface HttpTransport {
  request(path: `/${string}`, request: HttpRequest): Promise<HttpResponse>
}
```

`ApiError` contains only `status`, safe `code`, safe `message` and optional `requestId`. The core never logs raw bodies. API modules use generated DTOs and fixed `/api/v1/...` paths.

- [ ] **Step 4: Implement the browser adapter and compatibility re-exports**

The browser adapter owns `fetch`, `credentials: 'same-origin'`, relative URL resolution and JSON decoding. It must not persist request bodies. Keep the old files as thin re-exports backed by one browser `ApiClient` so pages remain unchanged during migration.

- [ ] **Step 5: Run core, adapter and existing Web tests**

Run:

```bash
nvm exec 22 npm run test:run --workspace @termflow/client-core
nvm exec 22 npm run test:run --workspace @termflow/web-client
nvm exec 22 npm run typecheck --workspace @termflow/client-core
nvm exec 22 npm run build:web
```

Expected: all core tests and the existing 86 Web tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/client-core apps/clients/web/src/api apps/clients/web/src/adapters
git commit -m "refactor(client): extract transport-neutral HTTP core"
```

### Task 4: DOM-free terminal session and browser WebSocket adapter

**Files:**

- Create: `packages/client-core/src/terminal/ports.ts`
- Create: `packages/client-core/src/terminal/session.ts`
- Create: `packages/client-core/src/terminal/session.test.ts`
- Create: `packages/client-core/src/terminal/modifiers.ts`
- Create: `packages/client-core/src/terminal/modifiers.test.ts`
- Create: `apps/clients/web/src/adapters/browserTerminalTransport.ts`
- Create: `apps/clients/web/src/adapters/browserTerminalTransport.test.ts`
- Modify: `apps/clients/web/src/terminal/socket.ts`
- Modify: `apps/clients/web/src/terminal/socket.test.ts`
- Delete: `apps/clients/web/src/terminal/protocol.ts`

- [ ] **Step 1: Write failing terminal core tests before moving behavior**

Use a fake `TerminalTransport` with explicit event delivery. Port shape:

```ts
export type TerminalTransportEvent =
  | { type: 'open' }
  | { type: 'text'; data: string }
  | { type: 'binary'; data: Uint8Array }
  | { type: 'close'; code: number }

export interface TerminalConnection {
  sendText(data: string): void
  sendBinary(data: Uint8Array): void
  close(code: number, reason: string): void
}

export interface TerminalTransport {
  connect(request: TerminalConnectRequest, emit: (event: TerminalTransportEvent) => void): TerminalConnection
}
```

Port every current socket test: ready gate, UTF-8 chunking, authoritative size, binding/action frames, replacement reset, reconnect backoff, exact resume cursor, detach, auth/origin close handling, malformed controls and stale terminal IDs.

- [ ] **Step 2: Run the new terminal tests and confirm the session is missing**

Run: `nvm exec 22 npm run test:run --workspace @termflow/client-core -- src/terminal/session.test.ts`

Expected: FAIL because `TerminalSession` does not exist.

- [ ] **Step 3: Implement the pure terminal state machine**

The core receives an injected scheduler, ID generator and transport. It never references `window`, `WebSocket`, `crypto`, `setTimeout`, DOM event types or Vue. Preserve the 65,536-byte binary limit, input gating, exponential reconnect capped at ten seconds, stream replacement reset and exact `(terminal_id, stream_id, after_seq)` resume tuple.

- [ ] **Step 4: Implement browser transport and compatibility wrapper**

The browser adapter alone maps `http/https` to `ws/wss`, creates `WebSocket`, converts DOM events, sets `binaryType='arraybuffer'`, and injects `crypto.randomUUID` plus browser timers. Keep `createTerminalSocket()` as a Web compatibility wrapper around the core until the UI migration completes.

- [ ] **Step 5: Prove the core has no platform imports**

Extend `tests/test_client_workspace_contract.py` to scan `packages/client-core/src` and reject these strings:

```python
for forbidden in ("from 'vue'", 'from "vue"', "window.", "document.", "localStorage", "new WebSocket", "@tauri"):
    assert forbidden not in source
```

- [ ] **Step 6: Run terminal and Web regression tests**

Run:

```bash
nvm exec 22 npm run test:run --workspace @termflow/client-contracts
nvm exec 22 npm run test:run --workspace @termflow/client-core
nvm exec 22 npm run test:run --workspace @termflow/web-client
.venv/bin/python -m pytest tests/test_client_workspace_contract.py -q
```

Expected: all tests PASS, including the migrated eleven socket behaviors.

- [ ] **Step 7: Commit**

```bash
git add packages/client-core packages/client-contracts apps/clients/web/src/terminal apps/clients/web/src/adapters tests/test_client_workspace_contract.py
git commit -m "refactor(client): extract terminal session core"
```

### Task 5: Shared Vue runtime, theme and pure presentation

**Files:**

- Create: `packages/client-ui/src/index.ts`
- Create: `packages/client-ui/src/runtime.ts`
- Create: `packages/client-ui/src/runtimeKey.ts`
- Create: `packages/client-ui/src/theme/theme.ts`
- Create: `packages/client-ui/src/theme/theme.test.ts`
- Move: `apps/clients/web/src/components/**` to `packages/client-ui/src/components/**`
- Move: `apps/clients/web/src/styles/**` to `packages/client-ui/src/styles/**`
- Move: `apps/clients/web/src/utils/time.ts` to `packages/client-ui/src/utils/time.ts`
- Move: `apps/clients/web/src/terminal/actions.ts` to `packages/client-ui/src/terminal/actions.ts`
- Move: `apps/clients/web/src/terminal/orientation.ts` to `packages/client-ui/src/terminal/orientation.ts`
- Move: `apps/clients/web/src/terminal/viewport.ts` to `packages/client-ui/src/terminal/viewport.ts`
- Move: `apps/clients/web/src/terminal/terminalAdapter.ts` to `packages/client-ui/src/terminal/xtermAdapter.ts`
- Move: matching unit tests beside the moved sources
- Modify: `apps/clients/web/src/stores/theme.ts`
- Modify: `apps/clients/web/src/main.ts`

- [ ] **Step 1: Write failing runtime and theme-port tests**

Define `ClientRuntime` as the only object UI code may use for API, terminal, clipboard, clock, visibility and canonical B URL. Define a separate `ThemePreferences` port:

```ts
export interface ThemePreferences {
  load(): ThemeId | null
  save(theme: ThemeId): void
}
```

Test that theme choice is applied through injected document-class operations and persisted only through `ThemePreferences`; credentials are not part of this interface.

- [ ] **Step 2: Run UI tests and confirm runtime injection is missing**

Run: `nvm exec 22 npm run test:run --workspace @termflow/client-ui`

Expected: FAIL because `ClientRuntime` and its Vue injection key do not exist.

- [ ] **Step 3: Implement runtime injection and move pure UI files**

Use `createClientUi(runtime)` to return a Vue plugin that provides the frozen runtime. Composables call `useClientRuntime()` and throw a fixed startup error when no runtime is installed. Move components and pure presentation utilities with `git mv`; change imports only, preserving markup, accessible labels, responsive behavior and design-token usage.

- [ ] **Step 4: Split browser theme persistence from shared theme state**

Move semantic theme selection to UI. Keep only `localStorage` and root-document class application in `apps/clients/web/src/adapters/browserThemePreferences.ts`. Do not move `packages/design-tokens`; UI imports it as the single CSS/token source.

- [ ] **Step 5: Run moved tests and Web regression**

Run:

```bash
nvm exec 22 npm run test:run --workspace @termflow/client-ui
nvm exec 22 npm run test:run --workspace @termflow/web-client
nvm exec 22 npm run typecheck
nvm exec 22 npm run build:web
```

Expected: the same component/a11y/responsive/theme behaviors PASS from their new package locations, and Web build PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/client-ui apps/clients/web/src packages/design-tokens
git commit -m "refactor(client): share Vue presentation and themes"
```

### Task 6: Shared dashboard, computer and login vertical slices

**Files:**

- Move: `apps/clients/web/src/views/DashboardView.vue` to `packages/client-ui/src/views/DashboardView.vue`
- Move: `apps/clients/web/src/views/ComputersView.vue` to `packages/client-ui/src/views/ComputersView.vue`
- Move: `apps/clients/web/src/views/LoginView.vue` to `packages/client-ui/src/views/LoginView.vue`
- Move: associated view tests to `packages/client-ui/src/views/`
- Move: `apps/clients/web/src/composables/useDashboard.ts` to `packages/client-ui/src/composables/useDashboard.ts`
- Move: `apps/clients/web/src/stores/session.ts` to `packages/client-ui/src/composables/useSession.ts`
- Create: `apps/clients/web/src/adapters/browserClipboard.ts`
- Create: `apps/clients/web/src/adapters/browserVisibility.ts`
- Modify: `packages/client-ui/src/components/computers/EnrollmentDialog.vue`
- Modify: `packages/client-ui/src/components/computers/ComputerNameEditor.vue`

- [ ] **Step 1: Rewrite page tests against a fake ClientRuntime and watch them fail**

The tests must stop stubbing global `fetch`, `navigator.clipboard`, `window.location` and document visibility. Mount each page with a fake runtime and assert the same rendered state and calls. Enrollment tests inject a fixed canonical URL, clock and clipboard.

- [ ] **Step 2: Run the three page test files and confirm direct imports still break isolation**

Run: `nvm exec 22 npm run test:run --workspace @termflow/client-ui -- src/views/DashboardView.test.ts src/views/ComputersView.test.ts src/views/LoginView.test.ts`

Expected: FAIL until the pages use runtime-provided use cases.

- [ ] **Step 3: Migrate the vertical slices**

Dashboard polling receives scheduler and visibility events from runtime. Computer rename and enrollment call runtime API use cases. Login owns only the token input value, calls runtime session login, clears the input in `finally`, and emits successful navigation without persisting the token.

- [ ] **Step 4: Add browser adapters and privacy scan**

Browser clipboard, visibility and canonical URL live in `apps/clients/web/src/adapters`. Extend the privacy contract so only browser theme preferences may reference `localStorage`; no client package may reference `sessionStorage` or `IndexedDB`.

- [ ] **Step 5: Run page, privacy and Web regression tests**

Run:

```bash
nvm exec 22 npm run test:run --workspace @termflow/client-ui
nvm exec 22 npm run test:run --workspace @termflow/web-client
nvm exec 22 npm run build:web
```

Expected: page behavior and privacy contracts PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/client-ui apps/clients/web/src
git commit -m "refactor(client): share dashboard computer and login flows"
```

### Task 7: Shared terminal page and xterm composition

**Files:**

- Move: `apps/clients/web/src/views/TerminalView.vue` to `packages/client-ui/src/views/TerminalView.vue`
- Move: `apps/clients/web/src/views/TerminalView.test.ts` to `packages/client-ui/src/views/TerminalView.test.ts`
- Move: `apps/clients/web/src/composables/useTerminalSession.ts` to `packages/client-ui/src/composables/useTerminalSession.ts`
- Move: `apps/clients/web/src/composables/usePointerViewport.ts` to `packages/client-ui/src/composables/usePointerViewport.ts`
- Move: associated composable tests to `packages/client-ui/src/composables/`
- Modify: `packages/client-ui/src/components/terminal/TerminalCanvas.vue`
- Modify: `packages/client-ui/src/terminal/xtermAdapter.ts`

- [ ] **Step 1: Write the failing injected-terminal page test**

Mount `TerminalView` with a fake runtime whose terminal factory records term ID, input, semantic actions and disposal. Assert the current terminal-only shell, Computer label, editable Term name, status, Pane/Window controls and route-leave disposal. Assert no runtime call can resize A.

- [ ] **Step 2: Run the terminal UI tests and confirm direct Web imports fail**

Run: `nvm exec 22 npm run test:run --workspace @termflow/client-ui -- src/views/TerminalView.test.ts`

Expected: FAIL until terminal/API/session access comes only from `ClientRuntime`.

- [ ] **Step 3: Migrate TerminalView and composables**

Keep xterm and DOM viewport logic in UI; use the core terminal session for connection, resume, input and semantic actions. The UI may fit, crop, pan and zoom but must never send a terminal resize command. Preserve confirmation for destructive Pane close and current mobile modifier behavior.

- [ ] **Step 4: Run all core/UI/Web tests and build**

Run:

```bash
nvm exec 22 npm run test:run --workspace @termflow/client-core
nvm exec 22 npm run test:run --workspace @termflow/client-ui
nvm exec 22 npm run test:run --workspace @termflow/web-client
nvm exec 22 npm run typecheck
nvm exec 22 npm run build:web
```

Expected: all commands PASS; current terminal behaviors remain covered.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui apps/clients/web/src packages/client-core
git commit -m "refactor(client): share terminal control UI"
```

### Task 8: Thin Web composition root and final migration gates

**Files:**

- Move: `apps/clients/web/src/App.vue` to `packages/client-ui/src/App.vue`
- Move: `apps/clients/web/src/views/NotFoundView.vue` to `packages/client-ui/src/views/NotFoundView.vue`
- Move: shared App, accessibility, responsive and privacy tests into `packages/client-ui/src/`
- Create: `packages/client-ui/src/router/routes.ts`
- Create: `apps/clients/web/src/runtime.ts`
- Create: `apps/clients/web/src/runtime.test.ts`
- Modify: `apps/clients/web/src/main.ts`
- Modify: `apps/clients/web/src/router.ts`
- Modify: `apps/clients/web/vite.config.ts`
- Modify: `apps/clients/web/tsconfig.app.json`
- Modify: `scripts/verify.sh`
- Modify: `scripts/run-web-e2e.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `apps/clients/README.md`
- Modify: `docs/web-client.md`
- Delete: all remaining Web-owned business API, store, view, component, composable, style and terminal-state files replaced by packages

- [ ] **Step 1: Write the failing thin-entry repository contract**

Extend `tests/test_client_workspace_contract.py` to ignore `*.test.ts` composition tests and assert that
the remaining production files under `apps/clients/web/src` contain only:

```python
{
    "env.d.ts",
    "main.ts",
    "router.ts",
    "runtime.ts",
    "adapters/browserHttpTransport.ts",
    "adapters/browserTerminalTransport.ts",
    "adapters/browserThemePreferences.ts",
    "adapters/browserClipboard.ts",
    "adapters/browserVisibility.ts",
}
```

Allow browser composition smoke tests, but reject business pages/components under the Web entry.

- [ ] **Step 2: Run the contract and confirm old Web business files remain**

Run: `.venv/bin/python -m pytest tests/test_client_workspace_contract.py -q`

Expected: FAIL with a list of remaining business files.

- [ ] **Step 3: Build the Web runtime and shared routes**

`apps/clients/web/src/runtime.ts` creates one core client with browser HTTP/terminal/theme/clipboard/visibility adapters. `main.ts` imports shared UI styles, installs the shared UI plugin, selects browser history and mounts shared `App`. Shared route records retain the current paths; Web router guards use the runtime session status use case.

- [ ] **Step 4: Remove compatibility shims and redundant dependencies**

Delete old Web API, DTO, terminal protocol/state, store, component, view, composable and shared CSS files after all imports point to packages. Remove Web dependencies now owned exclusively by UI/core. Keep `index.html`, Vite/TS config, Playwright config and E2E in Web.

- [ ] **Step 5: Update local/CI verification**

Make `scripts/verify.sh` run root `npm ci`, contract drift, all workspace tests/typechecks and Web build before Python gates. CI must install Node 22 and use the same root commands. `run-web-e2e.sh` must start the build produced by `@termflow/web-client` without installing a nested lock.

- [ ] **Step 6: Run full foundation verification**

Run:

```bash
source /home/mcocdaa/.nvm/nvm.sh
nvm exec 22 npm ci
nvm exec 22 npm run contracts:check
nvm exec 22 npm run test:run
nvm exec 22 npm run typecheck
nvm exec 22 npm run build:web
.venv/bin/python -m pytest tests/test_client_workspace_contract.py tests/contracts -q
.venv/bin/python -m pytest -q
```

Expected: contract drift clean; all workspace tests PASS; Web production build PASS; all 259 existing Python tests plus new contract tests PASS. Run Python TestClient/WebSocket tests outside the restricted thread sandbox.

- [ ] **Step 7: Run Web E2E**

Run: `bash scripts/run-web-e2e.sh`

Expected: current authenticated control-center flows PASS against an isolated local B; no external B or user data is touched.

- [ ] **Step 8: Commit**

```bash
git add --all
git commit -m "refactor(client): make Web C a thin shared-client entry"
```

## Completion boundary

This plan is complete only when current Web C behavior is preserved from the new package boundaries and the thin-entry contract passes. Completion does **not** mean that B TOTP/OAuth/DPoP, Docker runtime minimization, Tauri, mobile builds or native secure storage are implemented. Those are separate plans built on this foundation.
