# v0.2.0 Terminal Agent Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the real Agent setup, conversation, and approval workflow beside the live terminal without recreating its session.

**Architecture:** A Term-scoped composable owns product orchestration, while `TerminalView` owns only panel visibility and URL synchronization. `AgentChatSession` becomes a host-neutral single-root component used by both the existing page and a responsive right sidecar.

**Tech Stack:** Vue 3 Composition API, Vue Router, TypeScript, Vitest, Vue Test Utils, shared client-core/contracts, Playwright.

---

**Dependency:** The product setup API plan must be reviewed and generated contracts must be stable.

**Execution constraint:** One UI implementer owns all files in this plan. Preserve dirty files; do not modify the existing dirty `apps/clients/web/e2e/agent-chat.spec.ts`. Do not commit/push/reset/clean.

### Task 1: Make `AgentChatSession` Host-Neutral

**Files:**

- Create: `packages/client-ui/src/components/agent/AgentChatSession.test.ts`
- Modify: `packages/client-ui/src/components/agent/AgentChatSession.vue`
- Modify: `packages/client-ui/src/components/agent/AgentApprovalPanel.vue`
- Modify: `packages/client-ui/src/views/AgentChatView.vue`
- Modify: `packages/client-ui/src/views/AgentChatView.test.ts`

- [ ] **Step 1: Write embedding RED tests**

```typescript
it('renders one root and omits page navigation in sidecar mode', () => {
  const wrapper = mount(AgentChatSession, {
    props: { conversationId: 'c1', enabled: true, variant: 'sidecar' },
  })
  expect(wrapper.element.parentElement?.children).toHaveLength(1)
  expect(wrapper.find('[data-testid="agent-overview-link"]').exists()).toBe(false)
})

```

Add `opens the approval tray and focuses a requested approval`: mount sidecar
mode with two approvals, invoke the exposed focus method for the second ID, and
assert `<details>.open === true` plus `document.activeElement` is the second
approval control.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/AgentChatSession.test.ts src/views/AgentChatView.test.ts
```

- [ ] **Step 3: Refactor the component boundary**

```typescript
const props = withDefaults(defineProps<{
  conversationId: string
  enabled: boolean
  variant?: 'page' | 'sidecar'
}>(), { variant: 'page' })

const emit = defineEmits<{
  close: []
  pendingCount: [count: number]
}>()
```

Use one root element. Page-only navigation stays in page variant. Sidecar uses a
native `<details>` approval tray. `AgentApprovalPanel` exposes:

```typescript
defineExpose<{ focusApproval(approvalId: string): void }>({ focusApproval })
```

No global `document.querySelector` is allowed.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/AgentChatSession.test.ts src/views/AgentChatView.test.ts \
  src/components/agent/AgentApprovalCard.test.ts
git diff --check
```

### Task 2: Handle Approval 428 With Platform-Safe Re-authentication

**Files:**

- Create: `packages/client-ui/src/components/agent/AgentSensitiveReauthDialog.vue`
- Create: `packages/client-ui/src/components/agent/AgentSensitiveReauthDialog.test.ts`
- Create: `packages/client-ui/src/composables/useSensitiveAuthorization.ts`
- Modify: `packages/client-ui/src/composables/useAgentApprovals.ts`
- Modify: `packages/client-ui/src/composables/useAgentApprovals.test.ts`
- Modify: `packages/client-ui/src/components/agent/AgentApprovalCard.vue`
- Modify: `packages/client-ui/src/components/agent/AgentApprovalCard.test.ts`
- Modify: `packages/client-ui/src/runtime.ts`
- Modify: `packages/client-core/src/auth/nativeAuthorization.ts`
- Modify: `packages/client-core/src/auth/nativeAuthorization.test.ts`
- Modify: `apps/clients/web/src/runtime.ts`
- Modify: `apps/clients/tauri/src/runtime.ts`

- [ ] **Step 1: Write re-auth RED tests**

Create tests proving stale approve opens one re-auth flow and retries the exact
decision at most once, cancel performs no retry, deny/revoke never request
freshness, and native mode never exposes root credential/TOTP fields to the
WebView. Assert a second 428 surfaces a stable error instead of looping.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/composables/useAgentApprovals.test.ts \
  src/components/agent/AgentSensitiveReauthDialog.test.ts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- \
  src/auth/nativeAuthorization.test.ts
```

- [ ] **Step 3: Add a platform-owned sensitive-auth port**

Extend `ClientRuntime` with:

```typescript
export interface SensitiveAuthorizationPort {
  readonly mode: 'browser-session' | 'native-oauth'
  authorizeNative?(signal?: AbortSignal): Promise<'authenticated' | 'cancelled'>
}
```

In browser mode, `useSensitiveAuthorization` opens the focused dialog; the
dialog submits the root credential and optional TOTP directly to the existing
session APIs, clears both fields in `finally`, and never calls a native port. In
native mode the dialog renders no credential fields and calls
`authorizeNative`, which invokes forced OAuth with server-recognized
`prompt=login`; refresh credentials are not accepted as fresh.
`useAgentApprovals` catches only 428
`approval_reauthentication_required`, invokes a shared
`useSensitiveAuthorization` helper, clears temporary dialog input, and retries
the original approval once. The same helper handles
`sensitive_action_reauthentication_required` for setup/activation.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/composables/useAgentApprovals.test.ts \
  src/components/agent/AgentSensitiveReauthDialog.test.ts \
  src/components/agent/AgentApprovalCard.test.ts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- \
  src/auth/nativeAuthorization.test.ts
git diff --check
```

### Task 3: Implement the Term Agent State Machine

**Files:**

- Create: `packages/client-ui/src/composables/useTermAgent.ts`
- Create: `packages/client-ui/src/composables/useTermAgent.test.ts`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts`

- [ ] **Step 1: Write state/race RED tests**

Create tests named `ignores stale setup responses after the term changes`,
`rejects a URL conversation owned by another binding`,
`keeps one idempotency key across a failed user retry`, and
`maps stable server reason codes without exposing raw detail`. Resolve requests
out of order, compare the two submitted UUIDs for equality, and assert raw server
detail never reaches the public error ref.

`submitSetup` and `activate` use `useSensitiveAuthorization` and retry at most
once after a matching 428. Ordinary readiness/load failures never trigger an
authentication flow.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/composables/useTermAgent.test.ts
```

- [ ] **Step 3: Implement a generation-fenced composable**

```typescript
export function useTermAgent(options: {
  termId: MaybeRefOrGetter<string>
  requestedConversationId: MaybeRefOrGetter<string | null>
}) {
  return {
    setup, profiles, panes, bindingId, conversations,
    selectedConversationId, pendingApprovalCount,
    loading, mutating, error,
    refresh, submitSetup, activate, selectConversation, retry,
  }
}
```

Every refresh aborts the previous request and increments a generation. Apply a
response only when its generation and Term still match. Validate conversation
ownership against the current Binding. Generate an idempotency UUID on the first
submit attempt and clear it only after success or explicit form reset.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/composables/useTermAgent.test.ts
git diff --check
```

### Task 4: Build the Product Setup Form

**Files:**

- Create: `packages/client-ui/src/components/agent/AgentSetupForm.vue`
- Create: `packages/client-ui/src/components/agent/AgentSetupForm.test.ts`
- Modify: `packages/client-ui/src/index.ts`

- [ ] **Step 1: Write validation RED tests**

Create tests named `requires at least one exact pane and explicit disclosure
consent`, `emits numeric topology revision and current fingerprint`, `never
renders endpoint key token or arbitrary JSON inputs`, and `retains user
selection after a server error`. Submit before and after checking consent,
inspect the emitted discriminated union, enumerate every rendered input name,
and update the error prop without remounting before checking selections.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/AgentSetupForm.test.ts
```

- [ ] **Step 3: Implement the discriminated form output**

```typescript
export type AgentSetupSelection =
  | {
      kind: 'existing'
      profileId: string
      paneIds: string[]
      topologyRevision: number
      disclosureFingerprint: string
      accepted: true
    }
  | {
      kind: 'new'
      profileDisplayName: string
      paneIds: string[]
      topologyRevision: number
      disclosureFingerprint: string
      accepted: true
    }
```

Props are the aggregate setup summary, profiles, panes, busy, and safe error.
Emit one `submit` selection only after local validation. Default Pane selection
is empty; do not offer `all_panes`.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/AgentSetupForm.test.ts
git diff --check
```

### Task 5: Build `TerminalAgentPanel`

**Files:**

- Create: `packages/client-ui/src/components/agent/TerminalAgentPanel.vue`
- Create: `packages/client-ui/src/components/agent/TerminalAgentPanel.test.ts`
- Modify: `packages/client-ui/src/index.ts`

- [ ] **Step 1: Write panel-state RED tests**

Use a table test for `loading`, `unconfigured`, `deployment_required`,
`activating`, and `unavailable`; for each state assert chat is absent and only
the matching panel view exists. Add tests that change the conversation ID and
assert the prior chat unmounts once, then emit pending count/readiness and assert
the panel forwards the exact primitive values.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/TerminalAgentPanel.test.ts
```

- [ ] **Step 3: Implement the panel contract**

```typescript
defineProps<{ termId: string; conversationId: string | null }>()
defineEmits<{
  close: []
  selectConversation: [conversationId: string | null]
  pendingCount: [count: number]
  readiness: [state: AgentSetupResponse['state']]
}>()
```

The panel chooses exactly one state view. It delegates setup to
`AgentSetupForm`, orchestration to `useTermAgent`, and conversation rendering to
keyed `AgentChatSession`.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/agent/TerminalAgentPanel.test.ts \
  src/composables/useTermAgent.test.ts
git diff --check
```

### Task 6: Integrate the Sidecar Without Recreating Terminal State

**Files:**

- Create: `packages/client-ui/src/components/agent/TerminalAgentToggle.vue`
- Create: `packages/client-ui/src/views/TerminalView.test.ts`
- Modify: `packages/client-ui/src/views/TerminalView.vue`
- Modify: `packages/client-ui/src/App.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/styles/terminal-responsive.css`
- Modify: `packages/client-ui/src/test/a11y-contract.test.ts`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts`

- [ ] **Step 1: Write route/session/a11y RED tests**

Create tests named `opens sidecar and changes only the agent query`, `changes
conversation without recreating useTerminalSession`, `restores focus to toggle
after close`, and `makes canvas and mobile key bar inert only in mobile overlay
mode`. Spy on the terminal-session factory and require one call across query
changes; assert route path/params are unchanged; assert exact active element and
inert attributes at desktop and mobile breakpoints.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/views/TerminalView.test.ts src/test/a11y-contract.test.ts
```

- [ ] **Step 3: Implement route and layout state**

`TerminalView` owns only `agentOpen`, the `route.query.agent` projection,
readiness/pending badges, and focus restoration. Use `router.replace()` for
conversation query changes and remove the query on close. Keep the existing
`term:${termId}` component key. Place `TerminalAgentToggle` in the existing
titlebar slot.

Desktop uses a flex/grid sibling sidecar clamped to `26rem..30rem`. Mobile uses
an absolute overlay, does not set `aria-modal`, and sets `inert` on the terminal
canvas/keybar host while open. Change the enabled mobile navigation layout from
three fixed columns to four.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/views/TerminalView.test.ts src/test/a11y-contract.test.ts \
  src/components/agent/TerminalAgentPanel.test.ts
git diff --check
```

### Task 7: Add Web Privacy and Router Regressions

**Files:**

- Create: `apps/clients/web/src/router.test.ts`
- Create: `apps/clients/web/src/test/privacy-contract.test.ts`

- [ ] **Step 1: Write RED tests**

Assert query mutation preserves the Terminal component instance and no Agent
draft/message/terminal output/token/provider credential/disclosure detail enters
the URL, localStorage, sessionStorage, or console.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/web-client -- \
  src/router.test.ts src/test/privacy-contract.test.ts
```

- [ ] **Step 3: Apply the minimal route/runtime fixes**

Keep the query allowlist to `agent=<conversation UUID>`. Sanitize or omit any
diagnostic logging that serializes request bodies or Agent state.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/web-client -- \
  src/router.test.ts src/test/privacy-contract.test.ts
git diff --check
```

### Task 8: UI Slice Verification

**Files:** Verify only.

- [ ] **Step 1: Run unit, type, and build gates**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run test:run --workspace @termflow/client-ui
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run test:run --workspace @termflow/web-client
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run typecheck --workspace @termflow/client-contracts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run typecheck --workspace @termflow/client-core
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run typecheck --workspace @termflow/client-ui
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run typecheck --workspace @termflow/web-client
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run build:web
git diff --check
```

- [ ] **Step 2: Record handoff evidence**

Record exact counts and build exit code. Browser E2E belongs to the final
acceptance plan; unit/type/build success alone does not prove live integration.
