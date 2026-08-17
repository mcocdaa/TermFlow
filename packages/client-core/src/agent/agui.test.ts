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

})
