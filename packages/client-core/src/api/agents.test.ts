import { describe, expect, it, vi } from 'vitest'
import { createAgentsApi, fetchEventsSince } from './agents'
import type { AgentEventListResponse } from '@termflow/client-contracts'

const CONVERSATION = '11111111-1111-4111-8111-111111111111'

describe('agents API', () => {
  it('maps capabilities and conversation endpoints to paths and methods', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const agents = createAgentsApi(request)

    await agents.capabilities()
    await agents.listConversations()
    await agents.createConversation({ binding_id: 'b', title: null })
    await agents.getConversation(`${CONVERSATION} /1`)

    expect(request.mock.calls).toEqual([
      ['/api/v1/agent/capabilities', {}],
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
