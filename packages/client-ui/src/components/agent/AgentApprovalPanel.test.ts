import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { resetApprovalEntryCache } from '../../composables/useAgentApprovals'
import AgentApprovalPanel from './AgentApprovalPanel.vue'

function approvalEntry(approvalId: string, overrides: Partial<ApprovalResponse> = {}): ApprovalResponse {
  return {
    approval_id: approvalId,
    binding_id: 'b1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 't1',
    canonical_hash: 'hash-12345678',
    state: 'pending',
    expires_at: '2099-01-01T00:00:00+00:00',
    decided_at: null,
    decision: null,
    auth_epoch: 1,
    created_at: '2026-08-12T00:00:00+00:00',
    pane_id: '%0',
    operation: 'rm',
    intent_summary: 'rm -rf /var/data',
    ...overrides,
  }
}

import { createMemoryHistory, createRouter } from 'vue-router'

function mountPanel(approvals: ApprovalResponse[], request: ReturnType<typeof vi.fn>): VueWrapper {
  const runtime: ClientRuntime = createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  })
  return mount(AgentApprovalPanel, {
    attachTo: document.body,
    props: { conversationId: 'conv-1' },
    global: { plugins: [createClientUi(runtime), router] },
  })
}

beforeEach(() => {
  resetApprovalEntryCache()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AgentApprovalPanel', () => {
  it('renders pending approvals with risk badges', async () => {
    const list = [
      approvalEntry('a1', { operation: 'rm', intent_summary: 'rm -rf /var/data' }),
      approvalEntry('a2', { operation: 'read', intent_summary: 'cat /etc/hosts' }),
    ]
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals?')) return { approvals: list }
      return {}
    })
    const wrapper = mountPanel(list, request)
    await flushPromises()

    const badges = wrapper.findAll('.agent-risk-badge')
    expect(badges).toHaveLength(2)
    expect(badges[0]?.text()).toBe('极高危')
    expect(badges[0]?.attributes('data-agent-risk-level')).toBe('4')
    expect(badges[1]?.text()).toBe('低风险')
    expect(badges[1]?.attributes('data-agent-risk-level')).toBe('1')

    wrapper.unmount()
  })

  it('requires secondary confirmation checkbox for Level 4 critical approvals', async () => {
    const criticalApproval = approvalEntry('a1', { operation: 'rm', intent_summary: 'rm -rf /tmp/build' })
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals?')) return { approvals: [criticalApproval] }
      if (url.includes('/decide')) return { ...criticalApproval, state: 'approved' }
      return {}
    })
    const wrapper = mountPanel([criticalApproval], request)
    await flushPromises()

    // Click approve button to open confirmation dialog
    const approveBtn = wrapper.get('[data-action="approve-approval"]')
    await approveBtn.trigger('click')

    // Confirm dialog should be open with critical warning
    const dialog = wrapper.get('[data-agent-approval-dialog]')
    expect(dialog.find('[data-agent-critical-box]').exists()).toBe(true)

    // Confirm button should be disabled until checkbox is checked
    const confirmBtn = wrapper.get('[data-action="approve-confirm"]')
    expect(confirmBtn.attributes('disabled')).toBeDefined()

    // Check the confirmation checkbox
    const checkbox = wrapper.get('[data-action="critical-confirm-checkbox"]')
    await checkbox.setValue(true)

    // Now confirm button should be enabled
    expect(confirmBtn.attributes('disabled')).toBeUndefined()

    // Click confirm to approve
    await confirmBtn.trigger('click')
    await flushPromises()

    // Decide API was called
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/agent/approvals/a1/decide'),
      expect.objectContaining({ method: 'POST' }),
    )

    wrapper.unmount()
  })

  it('allows one-click confirmation without checkbox for non-critical approvals', async () => {
    const safeApproval = approvalEntry('a2', { operation: 'read', intent_summary: 'cat /etc/os-release' })
    const request = vi.fn(async (path: unknown) => {
      const url = String(path)
      if (url.startsWith('/api/v1/agent/approvals?')) return { approvals: [safeApproval] }
      if (url.includes('/decide')) return { ...safeApproval, state: 'approved' }
      return {}
    })
    const wrapper = mountPanel([safeApproval], request)
    await flushPromises()

    await wrapper.get('[data-action="approve-approval"]').trigger('click')

    const dialog = wrapper.get('[data-agent-approval-dialog]')
    expect(dialog.find('[data-agent-critical-box]').exists()).toBe(false)

    const confirmBtn = wrapper.get('[data-action="approve-confirm"]')
    expect(confirmBtn.attributes('disabled')).toBeUndefined()

    await confirmBtn.trigger('click')
    await flushPromises()

    expect(request).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/agent/approvals/a2/decide'),
      expect.objectContaining({ method: 'POST' }),
    )

    wrapper.unmount()
  })
})
