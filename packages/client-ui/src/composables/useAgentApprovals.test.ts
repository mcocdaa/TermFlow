import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { ApiError } from '@termflow/client-core'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import { useAgentApprovals } from './useAgentApprovals'
import { useBottomToast, type BottomToastController } from './useBottomToast'

function approval(overrides: Partial<ApprovalResponse> = {}): ApprovalResponse {
  return {
    approval_id: 'approval-1',
    binding_id: 'binding-1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 'tool-1',
    canonical_hash: 'hash-1',
    state: 'pending',
    expires_at: '2026-08-13T00:00:00+00:00',
    decided_at: null,
    decision: null,
    auth_epoch: 7,
    created_at: '2026-08-12T00:00:00+00:00',
    pane_id: 'pane-1',
    operation: null,
    intent_summary: null,
    ...overrides,
  }
}

interface ApprovalsHarness {
  approvals(): ReturnType<typeof useAgentApprovals>
  toast(): BottomToastController
  request: ReturnType<typeof vi.fn>
  unmount(): void
}

function mountApprovals(options: { conversationId?: string, request?: ReturnType<typeof vi.fn> } = {}): ApprovalsHarness {
  const request = options.request ?? vi.fn(async () => ({ approvals: [] }))
  const runtime = createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
  })
  let toast: BottomToastController | undefined
  let state: ReturnType<typeof useAgentApprovals> | undefined
  const wrapper = mount(defineComponent({
    setup() {
      toast = useBottomToast()
      state = useAgentApprovals(options.conversationId === undefined ? {} : { conversationId: options.conversationId })
      return () => h('div')
    },
  }), { global: { plugins: [createClientUi(runtime)] } })
  return {
    approvals: () => {
      if (state === undefined) throw new Error('approvals not captured')
      return state
    },
    toast: () => {
      if (toast === undefined) throw new Error('toast not captured')
      return toast
    },
    request,
    unmount: () => wrapper.unmount(),
  }
}

async function mounted(options: Parameters<typeof mountApprovals>[0] = {}) {
  const harness = mountApprovals(options)
  await flushPromises()
  return harness
}

function conflict(code: string, status = 409): ApiError {
  return new ApiError('validation', { status, code })
}

describe('useAgentApprovals', () => {
  it('loads the list on mount, optionally scoped to one conversation', async () => {
    const request = vi.fn(async (_path: string) => ({ approvals: [approval()] }))
    const scoped = await mounted({ conversationId: 'conv-1', request })
    expect(request.mock.calls[0]?.[0]).toBe('/api/v1/agent/approvals?conversation_id=conv-1')
    expect(scoped.approvals().approvals.value).toEqual([approval()])

    const global = await mounted({ request: vi.fn(async (_path: string) => ({ approvals: [] })) })
    expect(global.request.mock.calls[0]?.[0]).toBe('/api/v1/agent/approvals')
  })

  it('decide success refreshes the list and toasts', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) return approval({ state: 'approved' })
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    await harness.approvals().decide('approval-1', 'approve')

    expect(request).toHaveBeenNthCalledWith(2, '/api/v1/agent/approvals/approval-1/decide', expect.objectContaining({ method: 'POST', body: { decision: 'approve' } }))
    expect(request.mock.calls.filter(([path]) => !path.endsWith('/decide'))).toHaveLength(2) // initial + refresh
    expect(harness.toast().current.value?.text).toBe('已批准。')
  })

  it.each([
    ['approval_already_decided', 409],
    ['approval_revoked', 409],
    ['approval_already_consumed', 409],
    ['approval_conflict', 409],
  ])('409 %s refreshes the list and toasts the state change', async (code, status) => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict(code, status)
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    await harness.approvals().decide('approval-1', 'approve')

    expect(request.mock.calls.filter(([path]) => !path.endsWith('/decide'))).toHaveLength(2)
    expect(harness.toast().current.value?.text).toBe('审批状态已变化，列表已刷新。')
  })

  it('410 expired refreshes the list and toasts expiry', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_expired', 410)
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    await harness.approvals().decide('approval-1', 'approve')

    expect(request.mock.calls.filter(([path]) => !path.endsWith('/decide'))).toHaveLength(2)
    expect(harness.toast().current.value?.text).toBe('该审批请求已过期。')
  })

  it('404 removes the entry locally instead of refreshing', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_not_found', 404)
      return { approvals: [approval(), approval({ approval_id: 'approval-2' })] }
    })
    const harness = await mounted({ request })
    await harness.approvals().decide('approval-1', 'approve')

    expect(harness.approvals().approvals.value.map((entry) => entry.approval_id)).toEqual(['approval-2'])
    expect(request.mock.calls.filter(([path]) => !path.endsWith('/decide'))).toHaveLength(1) // no refresh
    expect(harness.toast().current.value?.text).toBe('该审批请求已不存在。')
  })

  it('409 approval_auth_epoch_stale prompts re-login without refreshing', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) throw conflict('approval_auth_epoch_stale', 409)
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    await harness.approvals().decide('approval-1', 'approve')

    expect(request.mock.calls.filter(([path]) => !path.endsWith('/decide'))).toHaveLength(1)
    expect(harness.toast().current.value?.text).toContain('重新登录')
  })

  it('tracks the in-flight aria-busy state and blocks duplicate decisions', async () => {
    const pending = new Promise<ApprovalResponse>(() => undefined)
    const decide = vi.fn(() => pending)
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/decide')) return decide()
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    const decidePromise = harness.approvals().decide('approval-1', 'approve')
    await flushPromises()

    expect(harness.approvals().isBusy('approval-1')).toBe(true)
    // A second click while the first is in flight must not reach the API.
    await harness.approvals().decide('approval-1', 'deny')
    expect(decide).toHaveBeenCalledTimes(1)
    void decidePromise
    harness.unmount()
  })

  it('revoke success refreshes and toasts', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/revoke')) return approval({ state: 'revoked' })
      return { approvals: [approval()] }
    })
    const harness = await mounted({ request })
    await harness.approvals().revoke('approval-1')

    expect(request).toHaveBeenNthCalledWith(2, '/api/v1/agent/approvals/approval-1/revoke', expect.objectContaining({ method: 'POST' }))
    expect(harness.toast().current.value?.text).toBe('已撤销审批。')
    expect(request.mock.calls.filter(([path]) => !path.endsWith('/revoke'))).toHaveLength(2)
  })
})
