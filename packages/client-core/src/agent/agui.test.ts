import { describe, expect, it } from 'vitest'
import { parseAguiEvent } from './agui'

const T = 1786622400000

describe('parseAguiEvent', () => {
  it('parses every projected kind with the pinned wire shapes', () => {
    expect(parseAguiEvent({ type: 'RUN_STARTED', threadId: 'c', runId: 'r', timestamp: T })).toEqual({
      type: 'RUN_STARTED', threadId: 'c', runId: 'r', timestamp: T,
    })
    expect(parseAguiEvent({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', role: 'assistant', delta: 'hi', timestamp: T })).toEqual({
      type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', role: 'assistant', delta: 'hi', timestamp: T,
    })
    expect(parseAguiEvent({ type: 'TEXT_MESSAGE_END', messageId: 'm' })).toEqual({
      type: 'TEXT_MESSAGE_END', messageId: 'm',
    })
    expect(parseAguiEvent({ type: 'TOOL_CALL_START', toolCallId: 't', toolCallName: 'read_file', timestamp: T })).toEqual({
      type: 'TOOL_CALL_START', toolCallId: 't', toolCallName: 'read_file', timestamp: T,
    })
    expect(parseAguiEvent({ type: 'TOOL_CALL_RESULT', messageId: 't', toolCallId: 't', role: 'tool', content: '{}', timestamp: T })).toEqual({
      type: 'TOOL_CALL_RESULT', messageId: 't', toolCallId: 't', role: 'tool', content: '{}', timestamp: T,
    })
    expect(parseAguiEvent({
      type: 'CUSTOM',
      name: 'termflow.permission_requested',
      value: { approval_request_id: 'a', tool_name: 'write_file', expires_at: '2026-08-13T13:00:00+00:00' },
      timestamp: T,
    })).toEqual({
      type: 'CUSTOM',
      name: 'termflow.permission_requested',
      value: { approval_request_id: 'a', tool_name: 'write_file', expires_at: '2026-08-13T13:00:00+00:00' },
      timestamp: T,
    })
    expect(parseAguiEvent({ type: 'RUN_FINISHED', threadId: 'c', runId: 'r', timestamp: T })).toEqual({
      type: 'RUN_FINISHED', threadId: 'c', runId: 'r', timestamp: T,
    })
    expect(parseAguiEvent({ type: 'RUN_ERROR', message: 'provider failed', code: 'E_RUN', timestamp: T })).toEqual({
      type: 'RUN_ERROR', message: 'provider failed', code: 'E_RUN', timestamp: T,
    })
    expect(parseAguiEvent({
      type: 'STATE_DELTA',
      delta: [{ op: 'replace', path: '/backend', value: { state: 'running', epoch: 7 } }],
      timestamp: T,
    })).toEqual({
      type: 'STATE_DELTA',
      delta: [{ op: 'replace', path: '/backend', value: { state: 'running', epoch: 7 } }],
      timestamp: T,
    })
  })

  it('accepts optional fields as absent', () => {
    expect(parseAguiEvent({ type: 'RUN_STARTED', threadId: 'c', runId: 'r' })).toEqual({
      type: 'RUN_STARTED', threadId: 'c', runId: 'r',
    })
    expect(parseAguiEvent({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', delta: 'hi' })).toEqual({
      type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', delta: 'hi',
    })
    expect(parseAguiEvent({ type: 'RUN_ERROR', message: 'boom' })).toEqual({
      type: 'RUN_ERROR', message: 'boom',
    })
  })

  it('returns null for unknown or malformed shapes', () => {
    const malformed: unknown[] = [
      null,
      42,
      'RUN_STARTED',
      [],
      {},
      { type: 'RUN_STARTED' },
      { type: 'UNKNOWN_KIND', x: 1 },
      { type: 7 },
      { type: 'RUN_STARTED', threadId: '', runId: 'r' },
      { type: 'RUN_STARTED', threadId: 'c', runId: 7 },
      { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', delta: '' },
      { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm' },
      { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', delta: 7 },
      { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm', delta: 'x', role: 7 },
      { type: 'TEXT_MESSAGE_END', messageId: '' },
      { type: 'TOOL_CALL_START', toolCallId: 't' },
      { type: 'TOOL_CALL_RESULT', messageId: 't', toolCallId: 't', content: 42 },
      { type: 'TOOL_CALL_RESULT', messageId: 't', toolCallId: 't', content: '{}', role: 7 },
      { type: 'CUSTOM', name: '', value: {} },
      { type: 'CUSTOM', name: 'x', value: [1] },
      { type: 'CUSTOM', name: 'x', value: 'not an object' },
      { type: 'RUN_FINISHED', threadId: 'c' },
      { type: 'RUN_ERROR', message: '' },
      { type: 'RUN_ERROR', message: 'x', code: 7 },
      { type: 'STATE_DELTA', delta: [] },
      { type: 'STATE_DELTA', delta: [{ op: 'replace' }] },
      { type: 'STATE_DELTA', delta: 'not an array' },
      { type: 'RUN_STARTED', threadId: 'c', runId: 'r', timestamp: 'soon' },
      { type: 'RUN_STARTED', threadId: 'c', runId: 'r', timestamp: Number.NaN },
    ]
    for (const value of malformed) {
      expect(parseAguiEvent(value), JSON.stringify(value)).toBeNull()
    }
  })

  it('never throws on hostile input', () => {
    const hostile: unknown[] = [
      { type: 'RUN_STARTED', threadId: { toString() { throw new Error('boom') } }, runId: 'r' },
      { type: 'CUSTOM', name: 'x', value: Object.create(null) },
      { type: 'STATE_DELTA', delta: [Object.create(null)] },
      { type: 'RUN_STARTED', get threadId() { throw new Error('getter boom') } },
    ]
    for (const value of hostile) {
      expect(() => parseAguiEvent(value)).not.toThrow()
    }
  })
})
