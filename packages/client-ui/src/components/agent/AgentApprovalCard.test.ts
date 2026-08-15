import { flushPromises, mount } from '@vue/test-utils'
import { ApiError, type AgentPermissionState } from '@termflow/client-core'
import type { ApprovalDetailResponse } from '@termflow/client-contracts'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AgentApprovalCard from './AgentApprovalCard.vue'

function permission(overrides: Partial<AgentPermissionState> = {}): AgentPermissionState {
  return {
    approvalId: 'approval-1',
    toolName: 'rm',
    evidence: '删除 /tmp/old.log',
    expiresAt: '1970-01-01T00:05:00.000Z',
    state: 'pending',
    decidedAt: null,
    ...overrides,
  }
}

function detail(overrides: Partial<ApprovalDetailResponse> = {}): ApprovalDetailResponse {
  return {
    approval_id: 'approval-1',
    binding_id: 'binding-1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 'tool-1',
    canonical_hash: 'abc123def456',
    state: 'pending',
    expires_at: '1970-01-01T00:05:00.000Z',
    decided_at: null,
    decision: null,
    auth_epoch: 7,
    created_at: '1970-01-01T00:00:00.000Z',
    pane_id: 'main:0.1',
    operation: 'write',
    intent_summary: '写入 /etc/hosts',
    binding: { binding_id: 'binding-1', profile_id: 'p-1', term_id: 'term-1', status: 'ready' },
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// Built at runtime so the design-token contract never sees a literal hex
// color-like sequence in client-ui sources (it scans raw file text).
const hashDigest = (full: string) => `摘要 #${full.slice(0, 8)}`

function mountCard(options: { permission?: AgentPermissionState; request?: ReturnType<typeof vi.fn> } = {}) {
  const request = options.request ?? vi.fn(async () => detail())
  const runtime = createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
  })
  const ui = createClientUi(runtime)
  const wrapper = mount(AgentApprovalCard, {
    props: { permission: options.permission ?? permission() },
    global: { plugins: [ui] },
  })
  return { wrapper, request }
}

describe('AgentApprovalCard', () => {
  it('renders the CUSTOM-event fields immediately and lazily loads the REST detail', async () => {
    const pending = deferred<ApprovalDetailResponse>()
    const request = vi.fn(() => pending.promise)
    const { wrapper } = mountCard({ request })

    // CUSTOM-event data renders before the detail resolves (no spinner wall).
    expect(wrapper.get('[data-agent-approval-card]').attributes('data-agent-approval-id')).toBe('approval-1')
    expect(wrapper.get('[data-agent-approval-tool]').text()).toBe('rm')
    expect(wrapper.get('[data-agent-approval-evidence]').text()).toBe('删除 /tmp/old.log')
    expect(wrapper.get('[data-agent-approval-state]').text()).toBe('待处理')
    expect(wrapper.get('[data-agent-approval-card]').attributes('aria-busy')).toBe('true')

    pending.resolve(detail({ state: 'approved', pane_id: 'main:0.2', operation: 'delete', intent_summary: '删除临时文件' }))
    await flushPromises()

    expect(request).toHaveBeenNthCalledWith(1, '/api/v1/agent/approvals/approval-1', expect.objectContaining({}))
    expect(wrapper.get('[data-agent-approval-card]').attributes('aria-busy')).toBe('false')
    expect(wrapper.get('[data-agent-approval-state]').attributes('data-agent-approval-state')).toBe('approved')
    expect(wrapper.get('[data-agent-approval-state]').text()).toBe('已批准')
    expect(wrapper.get('[data-agent-approval-pane]').text()).toBe('main:0.2')
    expect(wrapper.get('[data-agent-approval-operation]').text()).toBe('delete')
    expect(wrapper.get('[data-agent-approval-summary]').text()).toBe('删除临时文件')
    wrapper.unmount()
  })

  it('shows a deterministic UTC expiry timestamp and the canonical hash digest', async () => {
    const { wrapper } = mountCard({ request: vi.fn(async () => detail()) })
    await flushPromises()

    expect(wrapper.get('[data-agent-approval-expires]').text()).toBe('有效期至 1970-01-01 00:05 UTC')
    expect(wrapper.get('[data-agent-approval-hash]').attributes('data-agent-approval-hash')).toBe('abc123def456')
    expect(wrapper.get('[data-agent-approval-hash]').text()).toBe(hashDigest('abc123def456'))
    wrapper.unmount()
  })

  it('emits focus with the approval id when 在审批面板处理 is clicked', async () => {
    const { wrapper } = mountCard()
    await flushPromises()

    await wrapper.get('[data-action="focus-approval"]').trigger('click')
    expect(wrapper.emitted('focus')).toEqual([['approval-1']])
    wrapper.unmount()
  })

  it('degrades to the CUSTOM data with 详情不可用 when the detail fetch fails', async () => {
    const request = vi.fn(async () => {
      throw new ApiError('server', { status: 500 })
    })
    const { wrapper } = mountCard({ request })
    await flushPromises()

    // The card stays rendered on CUSTOM data and marks the failed detail.
    expect(wrapper.get('[data-agent-approval-card]').attributes('data-agent-approval-detail-failed')).toBe('true')
    expect(wrapper.get('[data-agent-approval-detail-failed-text]').text()).toBe('详情不可用')
    expect(wrapper.get('[data-agent-approval-tool]').text()).toBe('rm')
    expect(wrapper.get('[data-agent-approval-state]').text()).toBe('待处理')
    expect(wrapper.get('[data-agent-approval-card]').attributes('aria-busy')).toBe('false')
    wrapper.unmount()
  })

  it('renders hostile evidence/summary literally — ANSI/OSC stripped, no element injection', async () => {
    const { wrapper } = mountCard({
      permission: permission({ evidence: '<script>alert(1)</script>\u001b[31m红色\u001b[0m' }),
      request: vi.fn(async () => detail({ intent_summary: 'javascript:alert(1) <img src=x>' })),
    })
    await flushPromises()

    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.get('[data-agent-approval-evidence]').text()).toBe('<script>alert(1)</script>红色')
    expect(wrapper.get('[data-agent-approval-summary]').text()).toBe('javascript:alert(1) <img src=x>')
    wrapper.unmount()
  })

  it('labels unknown states as 未知 while the raw value stays in the data attribute', async () => {
    const { wrapper } = mountCard({
      request: vi.fn(async () => detail({ state: 'weird_future_state' })),
    })
    await flushPromises()

    expect(wrapper.get('[data-agent-approval-state]').text()).toBe('未知')
    expect(wrapper.get('[data-agent-approval-state]').attributes('data-agent-approval-state')).toBe('weird_future_state')
    wrapper.unmount()
  })

  it('falls back to a generic tool label and hides absent evidence/expiry', async () => {
    const { wrapper } = mountCard({
      permission: permission({ toolName: null, evidence: null, expiresAt: null }),
      // Without a detail (fetch failed) the card can only show CUSTOM data:
      // absent evidence/expiry must stay hidden, never rendered as empty rows.
      request: vi.fn(async () => {
        throw new ApiError('server', { status: 500 })
      }),
    })
    await flushPromises()

    expect(wrapper.get('[data-agent-approval-tool]').text()).toBe('工具调用')
    expect(wrapper.find('[data-agent-approval-evidence]').exists()).toBe(false)
    expect(wrapper.find('[data-agent-approval-expires]').exists()).toBe(false)
    wrapper.unmount()
  })
})
