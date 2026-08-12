import { describe, expect, it } from 'vitest'
import { parseAgentStreamFrame } from './frames'

const EVENT = {
  event_id: '11111111-1111-4111-8111-111111111111',
  conversation_id: '22222222-2222-4222-8222-222222222222',
  run_id: null,
  event_kind: 'message_delta',
  database_seq: 3,
  payload_digest: 'digest-3',
  ephemeral: false,
  created_at: '2026-08-12T00:00:00+00:00',
}

function frame(eventName: string, payload: unknown): string {
  return `event: ${eventName}\ndata: ${JSON.stringify(payload)}\n\n`
}

describe('parseAgentStreamFrame', () => {
  it('parses an agent_event frame into the transport event with its cursor', () => {
    const parsed = parseAgentStreamFrame(frame('agent_event', { type: 'event', event: EVENT, cursor: '7-3' }))
    expect(parsed).toEqual({ type: 'event', event: EVENT, cursor: '7-3' })
  })

  it('parses a reset frame (cursor_too_old) with the fresh opaque cursor', () => {
    const parsed = parseAgentStreamFrame(frame('reset', { type: 'reset', reason: 'cursor_too_old', cursor: '7-3' }))
    expect(parsed).toEqual({ type: 'reset', cursor: '7-3' })
  })

  it('parses a closed frame with the in-band code and reason', () => {
    const parsed = parseAgentStreamFrame(frame('closed', { type: 'closed', code: 4412, reason: 'binding_revoked' }))
    expect(parsed).toEqual({ type: 'close', code: 4412, reason: 'binding_revoked' })
  })

  it('accepts a zero sequence component in the opaque cursor', () => {
    const parsed = parseAgentStreamFrame(frame('reset', { type: 'reset', reason: 'cursor_too_old', cursor: '7-0' }))
    expect(parsed).toEqual({ type: 'reset', cursor: '7-0' })
  })

  it('rejects frames without an event name or data line', () => {
    expect(parseAgentStreamFrame('data: {}\n\n')).toBeNull()
    expect(parseAgentStreamFrame('event: agent_event\n\n')).toBeNull()
  })

  it('rejects malformed JSON and unknown event names without throwing', () => {
    expect(parseAgentStreamFrame(frame('agent_event', '{broken'))).toBeNull()
    expect(parseAgentStreamFrame(frame('agent.unknown', { type: 'event' }))).toBeNull()
  })

  it('rejects agent_event frames with an invalid cursor or event fields', () => {
    expect(parseAgentStreamFrame(frame('agent_event', { type: 'event', event: EVENT, cursor: 'x-y' }))).toBeNull()
    expect(parseAgentStreamFrame(frame('agent_event', { type: 'event', event: EVENT, cursor: '7-3-4' }))).toBeNull()
    const broken = { ...EVENT, database_seq: -1 }
    expect(parseAgentStreamFrame(frame('agent_event', { type: 'event', event: broken, cursor: '7-3' }))).toBeNull()
    const { database_seq: _omitted, ...missingSeq } = EVENT
    void _omitted
    expect(parseAgentStreamFrame(frame('agent_event', { type: 'event', event: missingSeq, cursor: '7-3' }))).toBeNull()
  })

  it('rejects reset frames whose cursor is malformed', () => {
    expect(parseAgentStreamFrame(frame('reset', { type: 'reset', reason: 'cursor_too_old', cursor: '0-3' }))).toBeNull()
    expect(parseAgentStreamFrame(frame('reset', { type: 'reset', reason: 'cursor_too_old' }))).toBeNull()
  })

  it('rejects closed frames without an integer code or reason', () => {
    expect(parseAgentStreamFrame(frame('closed', { type: 'closed', code: '4412', reason: 'x' }))).toBeNull()
    expect(parseAgentStreamFrame(frame('closed', { type: 'closed', code: 4412, reason: '' }))).toBeNull()
  })
})
