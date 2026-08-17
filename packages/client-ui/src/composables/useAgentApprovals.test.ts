import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { ApiError } from '@termflow/client-core'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime, type ClientUiPlugin } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import { useAgentApprovals } from './useAgentApprovals'

function approvalEntry(approvalId: string, overrides: Partial<ApprovalResponse> = {}): ApprovalResponse {
  return {
    approval_id: approvalId,
    binding_id: 'b1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 't1',
    canonical_hash: 'hash',
    state: 'pending',
    expires_at: '2026-08-12T00:00:00+00:00',
    decided_at: null,
    decision: null,
    auth_epoch: 1,
    created_at: '2026-08-12T00:00:00+00:00',
    pane_id: 'p1',
    operation: 'rm',
    intent_summary: '删除文件',
    ...overrides,
  }
}

interface Deferred<T> extends Promise<T> {
  resolve(value: T): void
  reject(error: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  }) as Deferred<T>
  promise.resolve = resolve
  promise.reject = reject
  return promise
}

interface ApprovalsHarness {
  wrapper: VueWrapper
  router: Router
  clientUi: ClientUiPlugin
  state(): ReturnType<typeof useAgentApprovals>
}

async function mounted(options: { request?: ReturnType<typeof vi.fn> } = {}): Promise<ApprovalsHarness> {
  const request = options.request ?? vi.fn(async () => ({ approvals: [] }))
  const runtime = createFakeRuntime({
    api: {
      ...createFakeRuntime().api,
      request,
    } as unknown as ClientRuntime['api'],
  })
  const clientUi = createClientUi(runtime)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  await router.push('/agent')
  await router.isReady()
  let exposed!: ReturnType<typeof useAgentApprovals>
  const harness = defineComponent({
    setup() {
      exposed = useAgentApprovals({ conversationId: 'conv-1' })
      return {}
    },
    render: () => null,
  })
  const wrapper = mount(harness, { attachTo: document.body, global: { plugins: [clientUi, router] } })
  await flushPromises()
  return { wrapper, router, clientUi, state: () => exposed }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('useAgentApprovals', () => {
  it('ignores a stale mount-time list that resolves after a newer refresh', async () => {
    const listCalls: Array<Deferred<{ approvals: ApprovalResponse[] }>> = []
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals')) {
        const pending = deferred<{ approvals: ApprovalResponse[] }>()
        listCalls.push(pending)
        return pending
      }
      return {}
    })
    const harness = await mounted({ request })
    // Mount started load #1 (in flight). A decide() success path refreshes
    // with a newer list (load #2)…
    const refreshPromise = harness.state().refresh()
    // …which resolves first, without the decided approval.
    listCalls[1]!.resolve({ approvals: [approvalEntry('a1', { state: 'approved' })] })
    await refreshPromise
    expect(harness.state().approvals.value.map((approval) => approval.approval_id)).toEqual(['a1'])
    expect(harness.state().approvals.value[0]?.state).toBe('approved')

    // The stale mount-time load resolves last with the outdated list — it
    // must not overwrite the newer data or resurrect handled approvals.
    listCalls[0]!.resolve({ approvals: [approvalEntry('a1'), approvalEntry('a2')] })
    await flushPromises()
    expect(harness.state().approvals.value.map((approval) => approval.approval_id)).toEqual(['a1'])
    expect(harness.state().approvals.value[0]?.state).toBe('approved')
    harness.wrapper.unmount()
  })

  it('does not resurrect approvals already handled by decide()', async () => {
    const listCalls: Array<Deferred<{ approvals: ApprovalResponse[] }>> = []
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.includes('/decide')) return approvalEntry('a1', { state: 'approved' })
      if (url.startsWith('/api/v1/agent/approvals')) {
        const pending = deferred<{ approvals: ApprovalResponse[] }>()
        listCalls.push(pending)
        return pending
      }
      return {}
    })
    const harness = await mounted({ request })

    const decidePromise = harness.state().decide('a1', 'approve')
    // Let the decide POST settle: its success path then starts the
    // post-decision refresh (load #2).
    await flushPromises()
    // decide succeeded; the refresh (load #2) resolves with the fresh
    // list (no a2, a1 already approved).
    listCalls[1]!.resolve({ approvals: [approvalEntry('a1', { state: 'approved' })] })
    await decidePromise
    expect(harness.state().approvals.value.map((approval) => approval.approval_id)).toEqual(['a1'])

    // The slow mount-time load resolves last with a1 still pending and a2
    // pending — the stale response must be dropped entirely.
    listCalls[0]!.resolve({ approvals: [approvalEntry('a1'), approvalEntry('a2')] })
    await flushPromises()
    expect(harness.state().approvals.value.map((approval) => approval.approval_id)).toEqual(['a1'])
    expect(harness.state().approvals.value[0]?.state).toBe('approved')
    harness.wrapper.unmount()
  })

  it('redirects to login on approval_auth_epoch_stale instead of leaving a stale session', async () => {
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.includes('/decide')) {
        throw new ApiError('server', { status: 409, code: 'approval_auth_epoch_stale' })
      }
      if (url.startsWith('/api/v1/agent/approvals')) return { approvals: [] }
      return {}
    })
    const harness = await mounted({ request })
    await harness.clientUi.session.loginWithToken('token')
    expect(harness.clientUi.session.sessionState.authenticated).toBe(true)

    await harness.state().decide('a1', 'approve')
    await flushPromises()

    // Same handling as the stream path (useAgentConversation 4401): the
    // session is cleared and the user is sent to /login with a redirect.
    expect(harness.clientUi.session.sessionState.authenticated).toBe(false)
    expect(harness.router.currentRoute.value.path).toBe('/login')
    expect(harness.router.currentRoute.value.query.redirect).toBe('/agent')
    harness.wrapper.unmount()
  })
})
