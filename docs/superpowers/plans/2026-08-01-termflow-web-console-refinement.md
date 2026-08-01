# TermFlow Web Console Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refine the B-hosted Web C login, dashboard, Computer management, named enrollment, local-time rendering, and navigation so they match the approved product behavior and selected rounded Term-card design.

**Architecture:** Keep B authoritative for UTC timestamps and single-use enrollment, but attach the user-selected Computer display name to the hashed enrollment grant until A consumes it. Keep presentation in Web C: the browser chooses its local timezone, route metadata chooses the bare login shell, and shared theme tokens drive interaction states.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic, Vue 3, TypeScript, Vitest, Playwright, Lucide Vue, Docker Compose.

---

## File map

- `packages/protocol/src/termflow_protocol/http.py`: optional display-name request contract.
- `apps/control-plane/src/termflow_control_plane/persistence/{models,repositories,database}.py`: persist and atomically consume a display name, including SQLite upgrade.
- `apps/control-plane/src/termflow_control_plane/api/enrollment.py`: issue and consume named enrollment grants.
- `apps/clients/web/src/api/computers.ts`: send the intended Computer name.
- `apps/clients/web/src/components/computers/EnrollmentDialog.vue`: two-stage enrollment and contained help.
- `apps/clients/web/src/{App.vue,router.ts,views/LoginView.vue}`: bare login route and Lucide navigation.
- `apps/clients/web/src/components/dashboard/{MetricCard,TermRow}.vue`: metric help and rounded Term cards.
- `apps/clients/web/src/components/computers/ComputerTable.vue`: four-column Chinese Computer table.
- `apps/clients/web/src/utils/time.ts`: C-local Chinese timestamps without zone suffix.
- `apps/clients/web/src/styles/app.css`: shared layout and interaction styling.
- `apps/clients/web/e2e/control-center.spec.ts`: real-browser acceptance.

### Task 1: Carry a validated Computer name through one-time enrollment

**Files:**
- Modify: `packages/protocol/src/termflow_protocol/http.py`
- Modify: `packages/protocol/src/termflow_protocol/__init__.py`
- Modify: `packages/protocol/tests/test_http_models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/database.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/enrollment.py`
- Modify: `apps/control-plane/tests/test_enrollment_api.py`
- Modify: `apps/control-plane/tests/test_repositories.py`

- [ ] **Step 1: Write failing protocol tests**

Import `EnrollmentCreateRequest`; assert `display_name="跑步工作站"` validates while empty,
129-character, and control-character names fail. Assert `EnrollmentCreateRequest()` remains valid
for CLI and legacy API compatibility.

- [ ] **Step 2: Run the protocol test and verify RED**

```bash
uv run --package termflow-protocol pytest packages/protocol/tests/test_http_models.py -q
```

Expected: import failure for missing `EnrollmentCreateRequest`.

- [ ] **Step 3: Add and export the request model**

```python
class EnrollmentCreateRequest(HttpModel):
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str | None) -> str | None:
        return validate_editable_name(value) if value is not None else None
```

- [ ] **Step 4: Verify protocol GREEN**

Run Step 2 and expect all selected tests to pass.

- [ ] **Step 5: Write failing repository and API tests**

Test that `create(..., display_name="跑步工作站")` followed by `consume(...)` returns that name,
concurrent consumption has exactly one winner, and a legacy `enrollment_tokens` table gains nullable
`display_name` after two idempotent initializations. POST `{"display_name":"跑步工作站"}`, enroll A
with hostname `devbox`, and assert hostname remains `devbox` while display name is `跑步工作站`.
Retain a no-body issue request that falls back to hostname.

- [ ] **Step 6: Run backend tests and verify RED**

```bash
uv run --package termflow-control-plane pytest apps/control-plane/tests/test_repositories.py apps/control-plane/tests/test_enrollment_api.py -q
```

Expected: create/consume signatures, database column, or display-name assertions fail.

- [ ] **Step 7: Implement named grants atomically**

Add nullable `display_name` to `EnrollmentToken` and to `_SQLITE_V2_COLUMNS`. Introduce an immutable
consumed-grant value containing `id` and `display_name`. Make repository creation accept an optional
name and make its conditional UPDATE return both columns in one statement. Accept
`EnrollmentCreateRequest | None` in the endpoint, persist the name, and create Installation with:

```python
display_name=consumed.display_name or request.hostname
```

- [ ] **Step 8: Verify GREEN and commit**

Run Step 6, then:

```bash
git add packages/protocol apps/control-plane
git commit -m "feat(control-plane): name computers during enrollment"
```

### Task 2: Build the two-stage Add Computer dialog

**Files:**
- Modify: `apps/clients/web/src/api/computers.ts`
- Modify: `apps/clients/web/src/components/computers/EnrollmentDialog.test.ts`
- Modify: `apps/clients/web/src/components/computers/EnrollmentDialog.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write failing dialog tests**

Assert the initial dialog has only `添加电脑`, a `电脑名称` input with `输入电脑名称`, and `创建`;
no request occurs before valid submission. Submit `跑步工作站` and assert request body
`{"display_name":"跑步工作站"}`. Assert stage two retains `添加电脑` and shows code, `终端执行`,
help, command, and copy. With fake timers, assert replacement reuses the name. Invalid names send no
request.

- [ ] **Step 2: Run and verify RED**

```bash
cd apps/clients/web && npm test -- EnrollmentDialog.test.ts --run
```

- [ ] **Step 3: Implement the flow**

Change `createEnrollmentCode` to accept an optional display name and serialize it. Add name, stage,
and validation state. Initial focus lands on the input; success advances stage and moves focus to a
meaningful code-stage control. Automatic expiry reuses the unchanged name. Rename the command-stage
heading to `终端执行`; preserve no-storage, focus trap, Escape, countdown, and copy behavior.

- [ ] **Step 4: Contain the tooltip**

Make `.enrollment-field-heading` the full-width positioning context. Position the tooltip below the
heading and bound its width to that context; remove the negative half-width translation.

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd apps/clients/web && npm test -- EnrollmentDialog.test.ts --run
git add apps/clients/web/src/api/computers.ts apps/clients/web/src/components/computers apps/clients/web/src/styles/app.css
git commit -m "feat(web): add named two-stage computer enrollment"
```

### Task 3: Simplify login and add navigation icons

**Files:**
- Modify: `apps/clients/web/src/App.test.ts`
- Modify: `apps/clients/web/src/views/LoginView.test.ts`
- Modify: `apps/clients/web/src/App.vue`
- Modify: `apps/clients/web/src/router.ts`
- Modify: `apps/clients/web/src/views/LoginView.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write failing tests**

At `/login`, assert no global header, theme picker, side nav, or mobile nav. Assert only the title,
token label/input, and `登录` button remain; old security eyebrow, storage note, and `创建会话` are
absent. On authenticated routes assert both navigation links contain Lucide SVGs with existing
accessible names.

- [ ] **Step 2: Run and verify RED**

```bash
cd apps/clients/web && npm test -- App.test.ts LoginView.test.ts --run
```

- [ ] **Step 3: Implement bare login and icons**

Set `meta: { bare: true }` on `/login`, derive `bareLayout` from the route, hide global chrome, and
make login main fill `100dvh`. Remove extra login copy and rename submit to `登录`, retaining safe
token handling. Import `LayoutDashboard` and `MonitorCog`; render them with `aria-hidden="true"`
before desktop and mobile nav text, styled through `currentColor`.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd apps/clients/web && npm test -- App.test.ts LoginView.test.ts --run
git add apps/clients/web/src/App.test.ts apps/clients/web/src/views/LoginView.test.ts apps/clients/web/src/App.vue apps/clients/web/src/router.ts apps/clients/web/src/views/LoginView.vue apps/clients/web/src/styles/app.css
git commit -m "feat(web): simplify login and navigation"
```

### Task 4: Add metric help and rounded Term cards

**Files:**
- Modify: `apps/clients/web/src/views/DashboardView.test.ts`
- Create: `apps/clients/web/src/components/dashboard/MetricCard.test.ts`
- Modify: `apps/clients/web/src/views/DashboardView.vue`
- Modify: `apps/clients/web/src/components/dashboard/MetricCard.vue`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write failing tests**

Assert online Terms and active Panes expose help through `aria-describedby` and keyboard focus.
Assert each Term row owns the rounded-card contract; online rows remain one link and offline rows
remain articles.

- [ ] **Step 2: Run and verify RED**

```bash
cd apps/clients/web && npm test -- MetricCard.test.ts DashboardView.test.ts --run
```

- [ ] **Step 3: Implement metric help**

Add optional `help`; when present, make the card focusable, connect a unique tooltip ID, and show it
on hover/focus-within. Pass dynamic copy from DashboardView, including total Terms in the online
help. Keep visible values unchanged.

- [ ] **Step 4: Implement selected option A**

Remove the list divider, add theme-token gaps, and give each Term row its own border,
`var(--radius-md)`, and elevated background. Online hover/focus adds an accent border, subtle shadow,
and one-pixel upward movement. Disable movement under reduced motion.

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd apps/clients/web && npm test -- MetricCard.test.ts DashboardView.test.ts --run
git add apps/clients/web/src/views/DashboardView* apps/clients/web/src/components/dashboard apps/clients/web/src/styles/app.css
git commit -m "feat(web): refine dashboard interactions"
```

### Task 5: Rebuild the Computer table and C-local time formatting

**Files:**
- Modify: `apps/clients/web/src/views/ComputersView.test.ts`
- Modify: `apps/clients/web/src/views/ComputersView.vue`
- Modify: `apps/clients/web/src/components/computers/ComputerTable.vue`
- Modify: `apps/clients/web/src/components/dashboard/StatusPill.vue`
- Create: `apps/clients/web/src/utils/time.test.ts`
- Modify: `apps/clients/web/src/utils/time.ts`
- Modify: `apps/clients/web/src/styles/app.css`

- [ ] **Step 1: Write failing tests**

Assert the only desktop headers are `名称`, `终端`, `最近在线`, `注册时间`; platform/version are
absent; three online Terms render one `在线 (3)` pill; and the B-time note is absent. Test the time
helper contains Chinese date/time fields but no `GMT`, `UTC`, or `CST`.

- [ ] **Step 2: Run and verify RED**

```bash
cd apps/clients/web && npm test -- ComputersView.test.ts time.test.ts --run
```

- [ ] **Step 3: Implement four columns and local time**

Render name/hostname, one count-aware status pill, last-seen time, and registered time. Extend
`StatusPill` with optional `label`, preserving defaults. Add responsive `data-label` values. Center
rows and cells vertically while keeping name contents left aligned. Use:

```ts
new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: 'long', day: 'numeric',
  hour: '2-digit', minute: '2-digit', hour12: false,
})
```

Do not pass `timeZone` or `timeZoneName`; remove the page time note.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd apps/clients/web && npm test -- ComputersView.test.ts time.test.ts --run
git add apps/clients/web/src/views/ComputersView* apps/clients/web/src/components/computers apps/clients/web/src/components/dashboard/StatusPill.vue apps/clients/web/src/utils apps/clients/web/src/styles/app.css
git commit -m "feat(web): localize computer management"
```

### Task 6: Browser acceptance, documentation, and delivery

**Files:**
- Modify: `apps/clients/web/e2e/control-center.spec.ts`
- Modify: `docs/security.md`
- Modify: `docs/web-client.md`

- [ ] **Step 1: Extend browser acceptance**

Drive bare login, dashboard, Computer page, and both enrollment stages. On desktop assert metric and
Term backgrounds change on hover, table cells share row vertical center, and tooltip bounds stay
inside dialog bounds. On mobile assert responsive labels and nav SVGs. Assert times have no timezone
suffix. Save desktop, portrait, and landscape screenshots.

- [ ] **Step 2: Run against a disposable B**

Build a unique image, create a unique data directory/volume and loopback port, seed one Computer/Term
fixture, then run:

```bash
TERMFLOW_E2E_BASE_URL=http://127.0.0.1:18765 \
TERMFLOW_E2E_ADMIN_TOKEN=termflow-refinement-e2e-admin \
TERMFLOW_E2E_TERM_ID=11111111-2222-4333-8444-555555555555 \
TERMFLOW_E2E_TERM_NAME=refinement-e2e \
TERMFLOW_E2E_SCREENSHOT_DIR=/tmp/termflow-refinement-e2e/screenshots \
npm --prefix apps/clients/web run e2e
```

Expected: all three viewport projects pass and existing port 8765 is untouched.

- [ ] **Step 3: Update current-behavior docs**

Document named enrollment metadata, raw-token secrecy, B UTC recording, and C-local rendering without
a timezone suffix.

- [ ] **Step 4: Run complete verification**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy packages/protocol/src apps/control-plane/src apps/node/src
npm --prefix apps/clients/web run test:run
npm --prefix apps/clients/web run typecheck
npm --prefix apps/clients/web run build
docker compose -f deploy/compose.yaml config --quiet
```

Expected: every command exits zero; dependency warnings are reported separately.

- [ ] **Step 5: Commit acceptance and docs**

```bash
git add apps/clients/web/e2e docs/security.md docs/web-client.md
git commit -m "test(web): verify refined control workflows"
```

- [ ] **Step 6: Safely replace B**

Build the exact verified commit to a unique image. Record the running container, image ID, restart
policy, healthcheck, volume, live metrics, and A connectivity. Rename only the exact old B container,
start the verified image with the same explicit volume and healthcheck, then verify `/healthz`, static
asset hashes, 60-second enrollment TTL, unchanged Computer/Term counts, and A reconnection. Preserve
the old container/image for rollback; do not delete user data.

- [ ] **Step 7: Final repository check**

```bash
git status --short --branch
git log -8 --oneline --decorate
```

Expected: clean intended branch and all task commits present.
