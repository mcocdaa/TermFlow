import { describe, expect, it, vi } from 'vitest'
import { createAgentsApi, fetchEventsSince, fetchEventsSinceAgui } from './agents'
import { ApiError } from '../http/apiError'
import type { AgentEventListResponse } from '@termflow/client-contracts'

const CONVERSATION = '11111111-1111-4111-8111-111111111111'

describe('agents API', () => {
  it('maps capabilities and conversation endpoints to paths and methods', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.capabilities()
    await agents.listBindings()
    await agents.listConversations()
    await agents.createConversation({ binding_id: 'b', title: null })
    await agents.getConversation(`${CONVERSATION} /1`)

    expect(request.mock.calls).toEqual([
      ['/api/v1/agent/capabilities', {}],
      ['/api/v1/agent/admin/bindings', {}],
      ['/api/v1/agent/conversations', {}],
      ['/api/v1/agent/conversations', { method: 'POST', body: { binding_id: 'b', title: null } }],
      [`/api/v1/agent/conversations/${encodeURIComponent(`${CONVERSATION} /1`)}`, {}],
    ])
  })

  it('maps history endpoints with query parameters', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.listMessages(CONVERSATION, { limit: 25, offset: 50 })
    await agents.listEvents(CONVERSATION, { since: 7, limit: 200 })

    expect(request.mock.calls).toEqual([
      [`/api/v1/agent/conversations/${CONVERSATION}/messages?limit=25&offset=50`, {}],
      [`/api/v1/agent/conversations/${CONVERSATION}/events?since=7&limit=200`, {}],
    ])
  })

  it('scopes the conversation list by binding and deletes with 204 semantics', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.listConversations({ bindingId: 'binding-1' })
    await agents.deleteConversation(CONVERSATION)

    expect(request.mock.calls).toEqual([
      ['/api/v1/agent/conversations?binding_id=binding-1', {}],
      [`/api/v1/agent/conversations/${CONVERSATION}`, { method: 'DELETE' }],
    ])
  })

  it('omits query parameters that are not provided', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.listEvents(CONVERSATION)
    await agents.listMessages(CONVERSATION)

    expect(request.mock.calls).toEqual([
      [`/api/v1/agent/conversations/${CONVERSATION}/events`, {}],
      [`/api/v1/agent/conversations/${CONVERSATION}/messages`, {}],
    ])
  })
})

describe('fetchEventsSince', () => {
  const event = (seq: number) => ({
    event_id: `22222222-2222-4222-8222-${String(seq).padStart(12, '0')}`,
    conversation_id: CONVERSATION,
    run_id: null,
    event_kind: 'message_delta',
    database_seq: seq,
    payload_digest: `digest-${seq}`,
    ephemeral: false,
    created_at: '2026-08-12T00:00:00+00:00',
  })

  it('pages until the cursor stops advancing', async () => {
    const page = (events: ReturnType<typeof event>[]): AgentEventListResponse => ({
      events,
      next_cursor: events.length === 0 ? 2 : events[events.length - 1]!.database_seq,
    })
    const request = vi.fn()
      .mockResolvedValueOnce(page([event(3), event(4)]))
      .mockResolvedValueOnce(page([event(5)]))
      .mockResolvedValueOnce(page([]))

    const events = await fetchEventsSince(request, CONVERSATION, 2)

    expect(events.map((ev) => ev.database_seq)).toEqual([3, 4, 5])
    expect(request.mock.calls.map(([path]) => path)).toEqual([
      `/api/v1/agent/conversations/${CONVERSATION}/events?since=2&limit=200`,
      `/api/v1/agent/conversations/${CONVERSATION}/events?since=4&limit=200`,
      `/api/v1/agent/conversations/${CONVERSATION}/events?since=5&limit=200`,
    ])
  })

  it('returns immediately when no events follow the cursor', async () => {
    const request = vi.fn().mockResolvedValueOnce({ events: [], next_cursor: 7 })
    const events = await fetchEventsSince(request, CONVERSATION, 7)
    expect(events).toEqual([])
    expect(request).toHaveBeenCalledTimes(1)
  })
})

describe('submitMessage and cancelRun', () => {
  it('posts admission and cancel bodies to the conversation endpoints', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.submitMessage(CONVERSATION, { text: 'hello' })
    await agents.cancelRun(CONVERSATION, { reason: 'user_cancelled' })

    expect(request.mock.calls).toEqual([
      [`/api/v1/agent/conversations/${CONVERSATION}/messages`, { method: 'POST', body: { text: 'hello' } }],
      [`/api/v1/agent/conversations/${CONVERSATION}/cancel`, { method: 'POST', body: { reason: 'user_cancelled' } }],
    ])
    const submitOptions = request.mock.calls[0]?.[1] as { body: Record<string, unknown> }
    expect(submitOptions.body).toEqual({ text: 'hello' })
    expect(submitOptions.body).not.toHaveProperty('draft_ref')
  })

  it('propagates ApiError details for the UI error mapping (503/409/422/403)', async () => {
    const cases: Array<{ call: () => Promise<unknown>, expected: Partial<ApiError> }> = [
      {
        call: () => createAgentsApi(vi.fn().mockRejectedValue(new ApiError('server', { status: 503, code: 'binding_runtime_unavailable' }))).submitMessage(CONVERSATION, { text: 'x' }),
        expected: { kind: 'server', status: 503, code: 'binding_runtime_unavailable' },
      },
      {
        call: () => createAgentsApi(vi.fn().mockRejectedValue(new ApiError('validation', { status: 409, code: 'no_active_run' }))).cancelRun(CONVERSATION, { reason: null }),
        expected: { kind: 'validation', status: 409, code: 'no_active_run' },
      },
      {
        call: () => createAgentsApi(vi.fn().mockRejectedValue(new ApiError('validation', { status: 422, code: 'invalid_request' }))).submitMessage(CONVERSATION, { text: '' }),
        expected: { kind: 'validation', status: 422, code: 'invalid_request' },
      },
      {
        call: () => createAgentsApi(vi.fn().mockRejectedValue(new ApiError('authentication', { status: 403, code: 'binding_revoked' }))).submitMessage(CONVERSATION, { text: 'x' }),
        expected: { kind: 'authentication', status: 403, code: 'binding_revoked' },
      },
    ]
    for (const testCase of cases) {
      await expect(testCase.call()).rejects.toMatchObject(testCase.expected)
    }
  })
})

describe('fetchEventsSinceAgui', () => {
  const chunk = (n: number) => ({ type: 'TEXT_MESSAGE_CHUNK', messageId: `m-${n}`, delta: `d-${n}` })

  it('pages while next_cursor advances and returns the watermark at termination', async () => {
    const page = (events: unknown[], next: number | null) => ({ events, next_cursor: next })
    const request = vi.fn()
      .mockResolvedValueOnce(page([chunk(3), chunk(4)], 4))
      .mockResolvedValueOnce(page([chunk(5)], 5))
      .mockResolvedValueOnce(page([], 5))

    const batch = await fetchEventsSinceAgui(request, CONVERSATION, 2)

    expect(batch.events).toEqual([chunk(3), chunk(4), chunk(5)])
    expect(batch.coveredThrough).toBe(5)
    expect(request.mock.calls.map(([path]) => path)).toEqual([
      `/api/v1/agent/conversations/${CONVERSATION}/events?wire=agui&since=2&limit=200`,
      `/api/v1/agent/conversations/${CONVERSATION}/events?wire=agui&since=4&limit=200`,
      `/api/v1/agent/conversations/${CONVERSATION}/events?wire=agui&since=5&limit=200`,
    ])
  })

  it('stops when next_cursor stops advancing even with a non-empty page', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ events: [chunk(3)], next_cursor: 4 })
      .mockResolvedValueOnce({ events: [chunk(4)], next_cursor: 4 })

    const batch = await fetchEventsSinceAgui(request, CONVERSATION, 3)
    expect(batch).toEqual({ events: [chunk(3), chunk(4)], coveredThrough: 4 })
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('returns an empty batch with coveredThrough = since when nothing follows', async () => {
    const request = vi.fn().mockResolvedValueOnce({ events: [], next_cursor: 7 })
    const batch = await fetchEventsSinceAgui(request, CONVERSATION, 7)
    expect(batch).toEqual({ events: [], coveredThrough: 7 })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('discards malformed or unknown event objects instead of throwing', async () => {
    const request = vi.fn().mockResolvedValueOnce({
      events: [chunk(3), { type: 'NOPE' }, null, { type: 'TEXT_MESSAGE_CHUNK', messageId: '', delta: '' }],
      next_cursor: 3,
    })
    const batch = await fetchEventsSinceAgui(request, CONVERSATION, 3)
    expect(batch.events).toEqual([chunk(3)])
    expect(batch.coveredThrough).toBe(3)
  })
})
