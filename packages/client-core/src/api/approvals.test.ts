import { describe, expect, it, vi } from 'vitest'
import { createApprovalsApi } from './approvals'

const APPROVAL = '44444444-4444-4444-4444-444444444444'
const CONVERSATION = '11111111-1111-4111-8111-111111111111'

describe('approvals API', () => {
  it('lists with an optional conversation filter', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const approvals = createApprovalsApi(request)

    await approvals.list()
    await approvals.list({ conversationId: CONVERSATION })

    expect(request.mock.calls).toEqual([
      ['/api/v1/agent/approvals', {}],
      [`/api/v1/agent/approvals?conversation_id=${CONVERSATION}`, {}],
    ])
  })

  it('maps detail/decide/revoke to their paths and methods', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const approvals = createApprovalsApi(request)

    await approvals.detail(APPROVAL)
    await approvals.decide(APPROVAL, 'approve')
    await approvals.decide(APPROVAL, 'deny')
    await approvals.revoke(APPROVAL)

    expect(request.mock.calls).toEqual([
      [`/api/v1/agent/approvals/${APPROVAL}`, {}],
      [`/api/v1/agent/approvals/${APPROVAL}/decide`, { method: 'POST', body: { decision: 'approve' } }],
      [`/api/v1/agent/approvals/${APPROVAL}/decide`, { method: 'POST', body: { decision: 'deny' } }],
      [`/api/v1/agent/approvals/${APPROVAL}/revoke`, { method: 'POST' }],
    ])
  })

  it('passes abort signals through to the request', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const approvals = createApprovalsApi(request)
    const controller = new AbortController()

    await approvals.list({ signal: controller.signal })
    await approvals.detail(APPROVAL, controller.signal)
    await approvals.decide(APPROVAL, 'approve', controller.signal)
    await approvals.revoke(APPROVAL, controller.signal)

    for (const call of request.mock.calls) {
      expect(call[1]).toMatchObject({ signal: controller.signal })
    }
  })
})
