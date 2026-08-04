# Computer Table Alignment and Delete Toast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the desktop Computers table use five aligned equal-width columns and show delete outcomes in a bottom-centered, auto-dismissing toast.

**Architecture:** Keep the table layout in the existing client-ui stylesheet and keep delete feedback local to `ComputersView.vue`. Use the injected runtime clock for deterministic three-second dismissal, preserve inline load/enrollment messages, and avoid introducing an application-wide notification service for this page-scoped requirement.

**Tech Stack:** Vue 3, TypeScript, CSS Grid, Vitest, Vue Test Utils, Vite, Docker Compose, Playwright/Chromium

---

## File map

- Modify `packages/client-ui/src/styles/app.css`: equal desktop grid, header/body alignment, responsive reset, and bottom-toast presentation.
- Modify `packages/client-ui/src/test/responsive-contract.test.ts`: CSS regression contract for equal columns, alignment, toast placement, and responsive reset.
- Modify `packages/client-ui/src/views/ComputersView.vue`: page-scoped delete notice state, live-region markup, timer lifecycle, and delete result routing.
- Modify `packages/client-ui/src/views/ComputersView.test.ts`: delete success/error notice behavior and deterministic dismissal.

### Task 1: Equal and align the desktop table columns

**Files:**
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `packages/client-ui/src/styles/app.css:73-78,243-245`

- [ ] **Step 1: Write the failing CSS contract**

Add these assertions beside the other `appCss` assertions in `responsive-contract.test.ts`:

```ts
expect(appCss).toContain(".computer-table-head, .computer-table-row { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));")
expect(appCss).toContain(".computer-table-head > :not(:first-child) { justify-self: center; text-align: center; }")
expect(appCss).toContain(".computer-table-row [role='cell']:not(:first-child) { align-items: center; text-align: center; }")
expect(appCss).toContain('.computer-table-actions { align-items: center; }')
expect(appCss).toMatch(/@media \(max-width: 64rem\)[\s\S]*\.computer-table-row \[role='cell'\]:not\(:first-child\) \{[^}]*align-items: flex-start;[^}]*text-align: start;/)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
```

Expected: FAIL because the stylesheet still contains unequal track sizes and a right-aligned Actions cell.

- [ ] **Step 3: Implement the minimum desktop and responsive CSS**

Replace the existing computer-table layout rules with:

```css
.computer-table-head, .computer-table-row { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); align-items: center; gap: var(--space-4); padding: var(--space-4); }
.computer-table-head { background: var(--color-elevated); color: var(--color-text-muted); font-size: 0.78rem; font-weight: 750; letter-spacing: 0.06em; }
.computer-table-head > :not(:first-child) { justify-self: center; text-align: center; }
.computer-table-row { border-block-start: 1px solid var(--color-border); }
.computer-table-row [role='cell'] { align-self: stretch; display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: var(--space-1); min-width: 0; }
.computer-table-row [role='cell']:not(:first-child) { align-items: center; text-align: center; }
.computer-table-actions { align-items: center; }
```

Inside `@media (max-width: 64rem)`, retain the current two-column row rule and add:

```css
.computer-table-row [role='cell']:not(:first-child) { align-items: flex-start; text-align: start; }
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit the alignment slice**

```bash
git add -- packages/client-ui/src/styles/app.css packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(web): align computer table columns"
```

### Task 2: Route delete outcomes to a bottom toast

**Files:**
- Modify: `packages/client-ui/src/views/ComputersView.test.ts`
- Modify: `packages/client-ui/src/views/ComputersView.vue`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Change the existing delete test to require a success toast**

Inject a controllable runtime clock, then replace the old inline-alert assertion with:

```ts
let dismissNotice: (() => void) | undefined
const clearTimeout = vi.fn()
const runtime = createFakeRuntime({
  api: { computers: { list, remove } } as unknown as ClientRuntime['api'],
  clock: {
    now: () => 0,
    setTimeout: (callback, delay) => { expect(delay).toBe(3_000); dismissNotice = callback; return 17 },
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => undefined,
  },
})

const notice = wrapper.get('[data-delete-notice]')
expect(notice.attributes('role')).toBe('status')
expect(notice.attributes('data-tone')).toBe('success')
expect(notice.text()).toBe('已删除')
expect(wrapper.find('.computers-view > .form-error').exists()).toBe(false)
dismissNotice?.()
await wrapper.vm.$nextTick()
expect(wrapper.find('[data-delete-notice]').exists()).toBe(false)
wrapper.unmount()
expect(clearTimeout).not.toHaveBeenCalled()
```

- [ ] **Step 2: Add the failing delete-error test**

Add a second test using an offline computer, `window.confirm` returning true, and `remove` rejecting:

```ts
it('shows a bottom error toast when deleting a Computer fails', async () => {
  const offlineComputer = { ...computer, installation_id: 'machine-offline', online: false, terms: [] }
  const list = vi.fn().mockResolvedValue({ computers: [offlineComputer] })
  const remove = vi.fn().mockRejectedValue(new Error('network failure'))
  const runtime = createFakeRuntime({ api: { computers: { list, remove } } as unknown as ClientRuntime['api'] })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const wrapper = mountComputers(runtime)
  await flushPromises()
  await wrapper.get('[data-action="delete-computer"]').trigger('click')
  await flushPromises()

  const notice = wrapper.get('[data-delete-notice]')
  expect(notice.attributes('role')).toBe('alert')
  expect(notice.attributes('data-tone')).toBe('error')
  expect(notice.text()).toBe('无法删除电脑。')
  expect(wrapper.find('[data-computer-id="machine-offline"]').exists()).toBe(true)
  confirm.mockRestore()
})
```

- [ ] **Step 3: Run the view tests and verify RED**

Run:

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/ComputersView.test.ts
```

Expected: FAIL because delete outcomes still use the top inline `message` and there is no dismiss timer.

- [ ] **Step 4: Implement page-scoped notice state and lifecycle**

In `ComputersView.vue`, render this after `EnrollmentDialog`:

```vue
<p
  v-if="deleteNotice"
  data-delete-notice
  class="computer-delete-toast"
  :data-tone="deleteNotice.tone"
  :role="deleteNotice.tone === 'error' ? 'alert' : 'status'"
>{{ deleteNotice.text }}</p>
```

Add the state and helpers:

```ts
type DeleteNotice = { text: string; tone: 'success' | 'error' }
const deleteNotice = ref<DeleteNotice | null>(null)
let deleteNoticeTimer: unknown | null = null

function clearDeleteNoticeTimer() {
  if (deleteNoticeTimer !== null) runtime.clock.clearTimeout(deleteNoticeTimer)
  deleteNoticeTimer = null
}

function showDeleteNotice(notice: DeleteNotice) {
  clearDeleteNoticeTimer()
  deleteNotice.value = notice
  deleteNoticeTimer = runtime.clock.setTimeout(() => {
    deleteNotice.value = null
    deleteNoticeTimer = null
  }, 3_000)
}
```

Route success and failure through `showDeleteNotice`:

```ts
showDeleteNotice({ text: '已删除', tone: 'success' })
// catch branch
showDeleteNotice({ text: error instanceof ApiError ? error.message : '无法删除电脑。', tone: 'error' })
```

Keep `message` for list/enrollment messages. Extend the existing unmount hook:

```ts
onBeforeUnmount(() => {
  controller.abort()
  clearDeleteNoticeTimer()
})
```

- [ ] **Step 5: Add the bottom-toast CSS**

Add:

```css
.computer-delete-toast { position: fixed; z-index: 70; inset-inline-start: 50%; inset-block-end: max(var(--space-5), env(safe-area-inset-bottom)); width: max-content; max-width: min(28rem, calc(100vw - 2 * var(--space-4))); margin: 0; transform: translateX(-50%); padding: var(--space-3) var(--space-5); border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-panel); box-shadow: var(--shadow-panel); text-align: center; }
.computer-delete-toast[data-tone='success'] { border-color: var(--color-online); color: var(--color-online); }
.computer-delete-toast[data-tone='error'] { border-color: var(--color-danger); color: var(--color-danger); }
```

Inside `@media (max-width: 47.99rem)`, add:

```css
.computer-delete-toast { inset-block-end: calc(5rem + env(safe-area-inset-bottom)); }
```

- [ ] **Step 6: Extend the CSS contract and verify GREEN**

Add assertions for fixed placement and the mobile offset:

```ts
expect(appCss).toContain('.computer-delete-toast { position: fixed; z-index: 70;')
expect(appCss).toContain(".computer-delete-toast[data-tone='success'] { border-color: var(--color-online); color: var(--color-online); }")
expect(appCss).toContain(".computer-delete-toast[data-tone='error'] { border-color: var(--color-danger); color: var(--color-danger); }")
expect(appCss).toMatch(/@media \(max-width: 47\.99rem\)[\s\S]*\.computer-delete-toast \{[^}]*inset-block-end: calc\(5rem \+ env\(safe-area-inset-bottom\)\);/)
```

Run both focused test files. Expected: PASS.

- [ ] **Step 7: Commit the toast slice**

```bash
git add -- packages/client-ui/src/views/ComputersView.vue packages/client-ui/src/views/ComputersView.test.ts packages/client-ui/src/styles/app.css packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(web): show delete feedback at page bottom"
```

### Task 3: Verify, deploy, and inspect the live UI

**Files:**
- No additional production files.
- Evidence: `/tmp/termflow-computers-aligned.png`, `/tmp/termflow-delete-toast.png`

- [ ] **Step 1: Run fresh client verification**

```bash
npm run test:run --workspace @termflow/client-ui
npm run typecheck --workspace @termflow/client-ui
npm run build:web
```

Expected: all tests pass, typecheck exits 0, and Vite creates `apps/clients/web/dist`.

- [ ] **Step 2: Rebuild and recreate the Compose service**

```bash
set -a
source .env
set +a
docker compose -f deploy/compose.yaml up -d --build --force-recreate control-plane
```

Do not pass `--volumes`; preserve `termflow-data`. Wait until `docker ps` reports `deploy-control-plane-1` healthy and `curl -fsS http://127.0.0.1:8765/healthz` returns `{"status":"ok"}`.

- [ ] **Step 3: Verify layout geometry in Chromium**

Log in using the deployed container's admin token, open `/computers` at `1600x1000`, and evaluate bounding boxes for the five headers and five cells in one row. Assert every track width differs from the first by at most one pixel, the first header/cell are left-aligned, the other four header/cell centers differ by at most one pixel, and the trash button center differs from the Actions header center by at most one pixel. Save `/tmp/termflow-computers-aligned.png`.

- [ ] **Step 4: Verify the toast without touching persisted computers**

In a fresh browser context, intercept the Computers list and DELETE requests with a disposable offline row. Confirm the delete dialog, complete the mocked DELETE, assert `[data-delete-notice]` is fixed, bottom-centered, contains `已删除`, and disappears after three seconds. Save `/tmp/termflow-delete-toast.png` while it is visible.

- [ ] **Step 5: Inspect final state**

```bash
git status --short
git log -5 --oneline
docker ps --filter name=deploy-control-plane-1 --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
```

Expected: no unrelated worktree changes, both implementation commits are present, and the current deployment is healthy on `127.0.0.1:8765`.
