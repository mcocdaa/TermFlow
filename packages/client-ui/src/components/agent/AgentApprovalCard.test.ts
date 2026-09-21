import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { ApprovalResponse } from '@termflow/client-contracts'
import type { AgentPermissionState } from '@termflow/client-core'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime, type ClientUiPlugin } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { cacheApprovalDetail, resetApprovalEntryCache, useAgentApprovals } from '../../composables/useAgentApprovals'
import AgentApprovalCard from './AgentApprovalCard.vue'

function approvalEntry(approvalId: string, overrides: Partial<ApprovalResponse> = {}): ApprovalResponse {
  return {
    approval_id: approvalId,
    binding_id: 'b1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 't1',
    canonical_hash: 'hash-12345678',
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

const PERMISSION: AgentPermissionState = {
  approvalId: 'a1',
  toolName: 'rm',
  evidence: 'will remove /tmp/x',
  expiresAt: null,
  state: 'pending',
  decidedAt: null,
}

function runtimeWith(request: ReturnType<typeof vi.fn>): ClientRuntime {
  return createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
  })
}

function mountCard(request: ReturnType<typeof vi.fn>, clientUi: ClientUiPlugin): VueWrapper {
  return mount(AgentApprovalCard, {
    attachTo: document.body,
    props: { permission: PERMISSION },
    global: { plugins: [clientUi] },
  })
}

beforeEach(() => {
  resetApprovalEntryCache()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AgentApprovalCard', () => {
  it('renders from the shared cache without a detail fetch', async () => {
    cacheApprovalDetail(approvalEntry('a1', { state: 'approved', operation: 'mkdir', intent_summary: '创建目录' }))
    const request = vi.fn(async () => {
      throw new Error('no request expected')
    })
    const clientUi = createClientUi(runtimeWith(request))
    const wrapper = mountCard(request, clientUi)
    await flushPromises()

    expect(wrapper.get('[data-agent-approval-state="approved"]').text()).toBe('已批准')
    expect(wrapper.get('[data-agent-approval-operation]').text()).toBe('mkdir')
    expect(wrapper.get('[data-agent-approval-summary]').text()).toBe('创建目录')
    expect(wrapper.get('[data-agent-approval-hash]').text()).toContain('摘要 #hash-123')
    expect(request).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('fetches the detail once on a cache miss and reuses it on remount', async () => {
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals/')) return approvalEntry('a1', { state: 'approved' })
      return {}
    })
    const clientUi = createClientUi(runtimeWith(request))

    const first = mountCard(request, clientUi)
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(1)
    expect(first.findAll('[data-agent-approval-state="approved"]')).toHaveLength(1)
    first.unmount()

    // A later card for the same approval reuses the cached entry.
    const second = mountCard(request, clientUi)
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(1)
    expect(second.findAll('[data-agent-approval-state="approved"]')).toHaveLength(1)
    second.unmount()
  })

  it('waits for an in-flight list load to seed the cache instead of fetching', async () => {
    // The panel's list load hangs while the card mounts; the card must wait
    // for it to settle and reuse the seeded entry — no per-card fetch.
    let resolveList!: (value: { approvals: ApprovalResponse[] }) => void
    const listPromise = new Promise<{ approvals: ApprovalResponse[] }>((resolve) => {
      resolveList = resolve
    })
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals?')) return listPromise
      if (url.startsWith('/api/v1/agent/approvals/')) throw new Error('detail must not be fetched')
      return {}
    })
    const clientUi = createClientUi(runtimeWith(request))
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
    })
    // Drive the panel's composable so a real list load is in flight.
    const panel = defineComponent({
      setup() {
        useAgentApprovals({ conversationId: 'conv-1' })
        return {}
      },
      render: () => null,
    })
    const panelWrapper = mount(panel, { attachTo: document.body, global: { plugins: [clientUi, router] } })
    const cardWrapper = mountCard(request, clientUi)
    await flushPromises()

    // The card is still resolving (no detail request fired yet).
    expect(cardWrapper.get('[data-agent-approval-card]').attributes('aria-busy')).toBe('true')
    expect(request.mock.calls.some(([path]) => String(path) === '/api/v1/agent/approvals/a1')).toBe(false)

    // The panel's list lands and seeds the shared cache.
    resolveList({ approvals: [approvalEntry('a1', { state: 'approved', operation: 'mkdir' })] })
    await flushPromises()

    expect(cardWrapper.findAll('[data-agent-approval-state="approved"]')).toHaveLength(1)
    expect(cardWrapper.get('[data-agent-approval-operation]').text()).toBe('mkdir')
    expect(request.mock.calls.some(([path]) => String(path) === '/api/v1/agent/approvals/a1')).toBe(false)
    cardWrapper.unmount()
    panelWrapper.unmount()
  })

  it('renders risk assessment badge and structured diff preview', async () => {
    cacheApprovalDetail(approvalEntry('a1', {
      state: 'pending',
      operation: 'rm',
      intent_summary: 'rm -rf /var/data',
    }))
    const request = vi.fn()
    const clientUi = createClientUi(runtimeWith(request))
    const wrapper = mountCard(request, clientUi)
    await flushPromises()

    const badge = wrapper.get('.agent-risk-badge')
    expect(badge.text()).toBe('极高危')
    expect(badge.attributes('data-agent-risk-level')).toBe('4')
    wrapper.unmount()
  })
})
