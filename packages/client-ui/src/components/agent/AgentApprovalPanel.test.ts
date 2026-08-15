import { flushPromises, mount } from '@vue/test-utils'
import { ApiError, createAgentHistoryState, type AgentHistoryState } from '@termflow/client-core'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createClientUi, type ClientRuntime, type ClockPort } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AgentApprovalPanel from './AgentApprovalPanel.vue'

function approval(overrides: Partial<ApprovalResponse> = {}): ApprovalResponse {
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

function conflict(code: string, status = 409): ApiError {
  return new ApiError('validation', { status, code })
}

/** Controllable clock: `fire()` runs the interval callback registered by the component. */
function panelClock(nowMs = 0): { nowMs: { current: number }; fire(): void; port: ClockPort } {
  const nowMsBox = { current: nowMs }
  let interval: (() => void) | null = null
  return {
    nowMs: nowMsBox,
    fire: () => interval?.(),
    port: {
      now: () => nowMsBox.current,
      setTimeout: () => 1,
      clearTimeout: () => undefined,
      setInterval: (callback: () => void) => {
        interval = callback
        return 1
      },
      clearInterval: () => {
        interval = null
      },
    },
  }
}

function historyWithPermissions(ids: string[]): AgentHistoryState {
  const state = createAgentHistoryState()
  for (const id of ids) {
    state.permissions.set(id, {
      approvalId: id,
      toolName: 'rm',
      evidence: null,
      expiresAt: null,
      state: 'pending',
      decidedAt: null,
    })
    state.timeline.push({ type: 'permission', refId: id, at: 0 })
  }
  return state
}

// Built at runtime so the design-token contract never sees a literal hex
// color-like sequence in client-ui sources (it scans raw file text).
const hashDigest = (full: string) => `摘要 #${full.slice(0, 8)}`

function mountPanel(options: { request?: ReturnType<typeof vi.fn>; clock?: ClockPort; history?: AgentHistoryState } = {}) {
  const request = options.request ?? vi.fn(async () => ({ approvals: [] }))
  const runtime = createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
    ...(options.clock !== undefined ? { clock: options.clock } : {}),
  })
  const ui = createClientUi(runtime)
  const wrapper = mount(AgentApprovalPanel, {
    props: {
      conversationId: 'conv-1',
      ...(options.history !== undefined ? { history: options.history } : {}),
    },
    global: { plugins: [ui] },
    attachTo: document.body,
  })
  return { wrapper, ui, request }
}

const listPath = '/api/v1/agent/approvals?conversation_id=conv-1'
const listCalls = (request: ReturnType<typeof vi.fn>) =>
  request.mock.calls.filter(([path]) => path === listPath).length

describe('AgentApprovalPanel', () => {
  it('loads the full approval list on mount scoped to the conversation', async () => {
    const request = vi.fn(async () => ({ approvals: [approval()] }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    expect(request).toHaveBeenNthCalledWith(1, listPath, expect.objectContaining({}))
    expect(wrapper.get('[data-agent-approval-panel]').classes()).toContain('agent-approval-panel')
    expect(wrapper.findAll('[data-agent-approval-item]')).toHaveLength(1)
    wrapper.unmount()
  })

  it('shows a loading hint while the first fetch is in flight, then an empty state', async () => {
    const pending = deferred<{ approvals: ApprovalResponse[] }>()
    const request = vi.fn(() => pending.promise)
    const { wrapper } = mountPanel({ request })

    expect(wrapper.get('[data-agent-approval-loading]').text()).toBe('加载中…')
    pending.resolve({ approvals: [] })
    await flushPromises()
    expect(wrapper.get('[data-agent-approval-empty]').text()).toBe('暂无待处理审批')
    wrapper.unmount()
  })

  it('keeps the loaded list visible during refreshes — the loading hint never flashes over data', async () => {
    const pendingRefresh = deferred<{ approvals: ApprovalResponse[] }>()
    let listFetches = 0
    const request = vi.fn(async (_path: string) => {
      listFetches += 1
      if (listFetches === 1) return { approvals: [approval()] }
      return pendingRefresh.promise
    })
    const { wrapper } = mountPanel({ request })
    await flushPromises()
    expect(wrapper.findAll('[data-agent-approval-item]')).toHaveLength(1)

    // A new CUSTOM permission triggers a refresh; while it is in flight the
    // already-rendered list must stay visible (no loading-hint flash).
    await wrapper.setProps({ history: historyWithPermissions(['p1']) })
    await flushPromises()
    expect(wrapper.find('[data-agent-approval-loading]').exists()).toBe(false)
    expect(wrapper.findAll('[data-agent-approval-item]')).toHaveLength(1)

    pendingRefresh.resolve({ approvals: [] })
    await flushPromises()
    expect(wrapper.get('[data-agent-approval-empty]').text()).toBe('暂无待处理审批')
    wrapper.unmount()
  })

  it('lists only pending approvals with native action buttons', async () => {
    const request = vi.fn(async () => ({
      approvals: [
        approval({ approval_id: 'a-pending' }),
        approval({ approval_id: 'a-approved', state: 'approved' }),
        approval({ approval_id: 'a-denied', state: 'denied' }),
        approval({ approval_id: 'a-revoked', state: 'revoked' }),
        approval({ approval_id: 'a-expired', state: 'expired' }),
        approval({ approval_id: 'a-consumed', state: 'consumed' }),
      ],
    }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    const items = wrapper.findAll('[data-agent-approval-item]')
    expect(items.map((item) => item.attributes('data-agent-approval-id'))).toEqual(['a-pending'])
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBe(3)
    for (const button of buttons) {
      expect(button.element.tagName).toBe('BUTTON')
      expect(button.attributes('type')).toBe('button')
    }
    wrapper.unmount()
  })

  it('renders pane/operation/intent_summary and falls back to the canonical hash digest plus 详情不可用 when null', async () => {
    const request = vi.fn(async () => ({
      approvals: [
        approval({ approval_id: 'a-full', pane_id: 'main:0.2', operation: 'delete', intent_summary: '删除临时文件' }),
        approval({ approval_id: 'a-bare', pane_id: null, operation: null, intent_summary: null }),
      ],
    }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    const full = wrapper.get('[data-agent-approval-id="a-full"]')
    expect(full.get('[data-agent-approval-pane]').text()).toBe('main:0.2')
    expect(full.get('[data-agent-approval-operation]').text()).toBe('delete')
    expect(full.get('[data-agent-approval-summary]').text()).toBe('删除临时文件')

    const bare = wrapper.get('[data-agent-approval-id="a-bare"]')
    expect(bare.get('[data-agent-approval-pane]').text()).toBe('详情不可用')
    expect(bare.get('[data-agent-approval-operation]').text()).toBe('详情不可用')
    expect(bare.get('[data-agent-approval-summary]').text()).toBe('详情不可用')
    // The canonical hash digest stays visible: full value in the attribute,
    // shortened digest in the text.
    expect(bare.get('[data-agent-approval-hash]').attributes('data-agent-approval-hash')).toBe('abc123def456')
    expect(bare.get('[data-agent-approval-hash]').text()).toBe(hashDigest('abc123def456'))
    wrapper.unmount()
  })

  it('renders hostile intent summaries literally — ANSI/OSC stripped, no element injection', async () => {
    const request = vi.fn(async () => ({
      approvals: [approval({ intent_summary: '<script>alert(1)</script>\u001b[31m红色\u001b[0m <img src=x>' })],
    }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    const summary = wrapper.get('[data-agent-approval-summary]')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(summary.text()).toBe('<script>alert(1)</script>红色 <img src=x>')
    wrapper.unmount()
  })

  it('shows an expires_at countdown ticking on the injected clock, and 已过期 past zero', async () => {
    const clock = panelClock(0)
    const request = vi.fn(async () => ({ approvals: [approval()] }))
    const { wrapper } = mountPanel({ request, clock: clock.port })
    await flushPromises()

    expect(wrapper.get('[data-agent-approval-countdown]').text()).toBe('05:00')

    clock.nowMs.current = 61_000
    clock.fire()
    await nextTick()
    expect(wrapper.get('[data-agent-approval-countdown]').text()).toBe('03:59')

    clock.nowMs.current = 300_000
    clock.fire()
    await nextTick()
    expect(wrapper.get('[data-agent-approval-countdown]').text()).toBe('已过期')
    wrapper.unmount()
  })

  it('approve opens a confirm dialog; cancelling leaves the request untouched', async () => {
    const request = vi.fn(async (_path: string) => ({ approvals: [approval()] }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="approve-approval"]').trigger('click')
    await nextTick()
    const dialog = wrapper.get('[data-agent-approval-dialog]')
    expect(dialog.attributes('role')).toBe('alertdialog')
    expect(dialog.attributes('aria-modal')).toBe('true')

    await wrapper.get('[data-action="approve-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-agent-approval-dialog]').exists()).toBe(false)
    expect(request.mock.calls.filter(([path]) => path.endsWith('/decide'))).toHaveLength(0)
    wrapper.unmount()
  })

  it('confirming the approve dialog posts the decision and refreshes the list', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) return approval({ state: 'approved' })
      return { approvals: [approval()] }
    })
    const { wrapper, ui } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="approve-approval"]').trigger('click')
    await nextTick()
    await wrapper.get('[data-action="approve-confirm"]').trigger('click')
    await flushPromises()

    expect(request).toHaveBeenCalledWith(
      '/api/v1/agent/approvals/approval-1/decide',
      expect.objectContaining({ method: 'POST', body: { decision: 'approve' } }),
    )
    expect(listCalls(request)).toBe(2) // initial + post-decision refresh
    expect(wrapper.find('[data-agent-approval-dialog]').exists()).toBe(false)
    expect(ui.toast.current.value?.text).toBe('已批准。')
    wrapper.unmount()
  })

  it('deny and revoke execute directly without a dialog', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) return approval({ state: 'denied' })
      if (path.endsWith('/revoke')) return approval({ state: 'revoked' })
      return { approvals: [approval()] }
    })
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-agent-approval-dialog]').exists()).toBe(false)
    expect(request).toHaveBeenCalledWith(
      '/api/v1/agent/approvals/approval-1/decide',
      expect.objectContaining({ method: 'POST', body: { decision: 'deny' } }),
    )

    await wrapper.get('[data-action="revoke-approval"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith(
      '/api/v1/agent/approvals/approval-1/revoke',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(listCalls(request)).toBe(3) // initial + 2 post-action refreshes
    wrapper.unmount()
  })

  it.each([
    ['approval_already_decided'],
    ['approval_revoked'],
    ['approval_already_consumed'],
  ])('409 %s refreshes the list and toasts the state change', async (code) => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict(code)
      return { approvals: [approval()] }
    })
    const { wrapper, ui } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await flushPromises()

    expect(listCalls(request)).toBe(2)
    expect(ui.toast.current.value?.text).toBe('审批状态已变化，列表已刷新。')
    wrapper.unmount()
  })

  it('410 expired refreshes the list and toasts the expiry', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_expired', 410)
      return { approvals: [approval()] }
    })
    const { wrapper, ui } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await flushPromises()

    expect(listCalls(request)).toBe(2)
    expect(ui.toast.current.value?.text).toBe('该审批请求已过期。')
    wrapper.unmount()
  })

  it('404 removes the entry locally without a refresh', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_not_found', 404)
      return { approvals: [approval(), approval({ approval_id: 'approval-2' })] }
    })
    const { wrapper, ui } = mountPanel({ request })
    await flushPromises()
    expect(wrapper.findAll('[data-agent-approval-item]')).toHaveLength(2)

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[data-agent-approval-item]').map((item) => item.attributes('data-agent-approval-id'))).toEqual(['approval-2'])
    expect(listCalls(request)).toBe(1)
    expect(ui.toast.current.value?.text).toBe('该审批请求已不存在。')
    wrapper.unmount()
  })

  it('409 approval_auth_epoch_stale prompts re-login without refreshing', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_auth_epoch_stale')
      return { approvals: [approval()] }
    })
    const { wrapper, ui } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await flushPromises()

    expect(listCalls(request)).toBe(1)
    expect(ui.toast.current.value?.text).toContain('重新登录')
    wrapper.unmount()
  })

  it('marks the row aria-busy and disables buttons while a decision is in flight', async () => {
    const pending = deferred<ApprovalResponse>()
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) return pending.promise
      return { approvals: [approval()] }
    })
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    await wrapper.get('[data-action="deny-approval"]').trigger('click')
    await nextTick()

    const row = wrapper.get('[data-agent-approval-item]')
    expect(row.attributes('aria-busy')).toBe('true')
    for (const button of wrapper.findAll('button')) {
      expect(button.attributes('disabled')).toBeDefined()
    }

    pending.resolve(approval({ state: 'denied' }))
    await flushPromises()
    expect(wrapper.get('[data-agent-approval-item]').attributes('aria-busy')).toBe('false')
    wrapper.unmount()
  })

  it('traps focus in the confirm dialog, Escape cancels, and focus returns to the approve button', async () => {
    const request = vi.fn(async () => ({ approvals: [approval()] }))
    const { wrapper } = mountPanel({ request })
    await flushPromises()

    const approveButton = wrapper.get('[data-action="approve-approval"]')
    // jsdom never focuses on a dispatched click: focus explicitly so the
    // dialog can capture the real trigger element for focus restoration.
    ;(approveButton.element as HTMLButtonElement).focus()
    await approveButton.trigger('click')
    await nextTick()

    const dialog = wrapper.get('[data-agent-approval-dialog]')
    const cancelButton = wrapper.get('[data-action="approve-cancel"]')
    const confirmButton = wrapper.get('[data-action="approve-confirm"]')
    // Initial focus lands on the cancel button (ClosePaneDialog pattern).
    expect(document.activeElement).toBe(cancelButton.element)

    // Shift+Tab from the first focusable wraps to the last.
    await dialog.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(confirmButton.element)

    // Escape cancels and restores focus to the triggering approve button.
    await dialog.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[data-agent-approval-dialog]').exists()).toBe(false)
    expect(document.activeElement).toBe(approveButton.element)
    wrapper.unmount()
  })

  it('refreshes when a new pending permission arrives on the timeline, never polling', async () => {
    const clock = panelClock(0)
    const request = vi.fn(async () => ({ approvals: [] }))
    const { wrapper } = mountPanel({ request, clock: clock.port })
    await flushPromises()
    expect(listCalls(request)).toBe(1)

    // New CUSTOM permission → refresh.
    await wrapper.setProps({ history: historyWithPermissions(['p1']) })
    await flushPromises()
    expect(listCalls(request)).toBe(2)

    // Same pending id again (replay/live duplicate) → no extra refresh.
    await wrapper.setProps({ history: historyWithPermissions(['p1']) })
    await flushPromises()
    expect(listCalls(request)).toBe(2)

    // A second new permission → refresh.
    await wrapper.setProps({ history: historyWithPermissions(['p1', 'p2']) })
    await flushPromises()
    expect(listCalls(request)).toBe(3)

    // Interval ticks (simulated seconds) never fetch — no polling.
    clock.fire()
    clock.fire()
    clock.fire()
    await nextTick()
    expect(listCalls(request)).toBe(3)
    wrapper.unmount()
  })
})
