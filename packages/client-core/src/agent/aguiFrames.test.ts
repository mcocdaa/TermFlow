import { describe, expect, it } from 'vitest'
import { parseAgentStreamFrameAgui } from './aguiFrames'

const FRAME = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`

describe('parseAgentStreamFrameAgui', () => {
  it('parses an agent_event frame with a projected AG-UI event and cursor', () => {
    const frame = FRAME('agent_event', {
      type: 'event',
      event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', role: 'assistant', delta: 'hi', timestamp: 1 },
      cursor: '7-3',
    })
    expect(parseAgentStreamFrameAgui(frame)).toEqual({
      type: 'event',
      event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', role: 'assistant', delta: 'hi', timestamp: 1 },
      cursor: '7-3',
    })
  })

  it('accumulates multiple data lines within one frame', () => {
    const partial = 'event: agent_event\ndata: {"type":"event","event":{"type":"RUN_STARTED","threadId":"c",\n'
    expect(parseAgentStreamFrameAgui(partial)).toBeNull()
    const complete = `${partial}data: "runId":"r"},"cursor":"7-1"}\n\n`
    expect(parseAgentStreamFrameAgui(complete)).toEqual({
      type: 'event',
      event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' },
      cursor: '7-1',
    })
  })

  it('rejects canonical event payloads and malformed agui events', () => {
    // A canonical AgentEventResponse has no AG-UI `type` discriminator.
    const canonical = FRAME('agent_event', {
      type: 'event',
      event: {
        event_id: '22222222-2222-4222-8222-222222222222',
        conversation_id: '11111111-1111-4111-8111-111111111111',
        run_id: null,
        event_kind: 'message_delta',
        database_seq: 3,
        payload_digest: 'd',
        ephemeral: false,
        created_at: '2026-08-12T00:00:00+00:00',
      },
      cursor: '7-3',
    })
    expect(parseAgentStreamFrameAgui(canonical)).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('agent_event', { type: 'event', event: { type: 'NOPE' }, cursor: '7-3' }))).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('agent_event', { type: 'event', cursor: '7-3' }))).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('agent_event', { type: 'event', event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' } }))).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('agent_event', { type: 'event', event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' }, cursor: '7--1' }))).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('agent_event', { type: 'event', event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' }, cursor: 7 }))).toBeNull()
  })

  it('parses reset frames identically to the canonical parser', () => {
    expect(parseAgentStreamFrameAgui(FRAME('reset', { type: 'reset', cursor: '7-9' }))).toEqual({
      type: 'reset', cursor: '7-9',
    })
    expect(parseAgentStreamFrameAgui(FRAME('reset', { type: 'reset', cursor: 'nope' }))).toBeNull()
  })

  it('parses closed frames identically to the canonical parser', () => {
    expect(parseAgentStreamFrameAgui(FRAME('closed', { type: 'closed', code: 4410, reason: 'stream_too_slow' }))).toEqual({
      type: 'close', code: 4410, reason: 'stream_too_slow',
    })
    expect(parseAgentStreamFrameAgui(FRAME('closed', { type: 'closed', code: 'x', reason: 'bad' }))).toBeNull()
    expect(parseAgentStreamFrameAgui(FRAME('closed', { type: 'closed', code: 1006, reason: '' }))).toBeNull()
  })

  it('returns null for unknown frames, bad JSON, and missing pieces', () => {
    expect(parseAgentStreamFrameAgui('event: nope\ndata: {}\n\n')).toBeNull()
    expect(parseAgentStreamFrameAgui('data: {"type":"event"}\n\n')).toBeNull()
    expect(parseAgentStreamFrameAgui('event: agent_event\n\n')).toBeNull()
    expect(parseAgentStreamFrameAgui('event: agent_event\ndata: not-json\n\n')).toBeNull()
    expect(parseAgentStreamFrameAgui('event: agent_event\ndata: [1,2]\n\n')).toBeNull()
    expect(parseAgentStreamFrameAgui('')).toBeNull()
  })
})
