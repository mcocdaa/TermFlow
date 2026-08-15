import { describe, expect, it } from 'vitest'
import type { AgentMessageResponse } from '@termflow/client-contracts'
import { applyAguiEvent, createAgentHistoryState, seedUserMessages, type AgentHistoryState } from './history'
import type { AguiEvent } from './agui'

const now = () => 1_000_000

const chunk = (messageId: string, delta: string, timestamp?: number): AguiEvent => ({
  type: 'TEXT_MESSAGE_CHUNK', messageId, delta, ...(timestamp === undefined ? {} : { timestamp }),
})
const end = (messageId: string): AguiEvent => ({ type: 'TEXT_MESSAGE_END', messageId })
const toolStart = (toolCallId: string, toolCallName: string): AguiEvent => ({
  type: 'TOOL_CALL_START', toolCallId, toolCallName,
})
const toolResult = (toolCallId: string, content: string, timestamp?: number): AguiEvent => ({
  type: 'TOOL_CALL_RESULT', messageId: toolCallId, toolCallId, content,
  ...(timestamp === undefined ? {} : { timestamp }),
})
const runStarted = (runId: string): AguiEvent => ({ type: 'RUN_STARTED', threadId: 'c', runId })
const runFinished = (runId: string): AguiEvent => ({ type: 'RUN_FINISHED', threadId: 'c', runId })
const runError = (message: string, code?: string): AguiEvent => ({ type: 'RUN_ERROR', message, ...(code === undefined ? {} : { code }) })
const permission = (approvalId: string, extras: Record<string, unknown> = {}): AguiEvent => ({
  type: 'CUSTOM',
  name: 'termflow.permission_requested',
  value: { approval_request_id: approvalId, ...extras },
})
const stateDelta = (state: string, epoch: number): AguiEvent => ({
  type: 'STATE_DELTA',
  delta: [{ op: 'replace', path: '/backend', value: { state, epoch } }],
})

const apply = (state: AgentHistoryState, ...events: AguiEvent[]) =>
  events.reduce((current, event) => applyAguiEvent(current, event, now), state)

describe('applyAguiEvent messages', () => {
  it('creates a streaming message on the first chunk and appends later chunks', () => {
    const state = apply(createAgentHistoryState(), chunk('m', 'Hel'), chunk('m', 'lo'))
    expect(state.messages.get('m')).toEqual({
      messageId: 'm', role: 'assistant', text: 'Hello', status: 'streaming', createdAt: now(),
    })
    expect(state.timeline).toEqual([{ type: 'message', refId: 'm', at: now() }])
  })

  it('completes on END and ignores chunks after complete', () => {
    const state = apply(createAgentHistoryState(), chunk('m', 'hi'), end('m'), chunk('m', 'late'))
    expect(state.messages.get('m')?.status).toBe('complete')
    expect(state.messages.get('m')?.text).toBe('hi')
    expect(state.timeline).toHaveLength(1)
  })

  it('ignores END for unknown message ids and repeated END is idempotent', () => {
    const base = apply(createAgentHistoryState(), chunk('m', 'hi'), end('m'))
    const repeated = applyAguiEvent(base, end('m'), now)
    expect(repeated).toEqual(base)
    const unknown = apply(base, end('ghost'))
    expect(unknown).toEqual(base)
  })

  it('keeps the event timestamp when present', () => {
    const state = apply(createAgentHistoryState(), chunk('m', 'hi', 42), end('m'))
    expect(state.messages.get('m')?.createdAt).toBe(42)
  })
})

describe('applyAguiEvent tool calls', () => {
  it('tracks running -> completed with the raw summary text', () => {
    const state = apply(
      createAgentHistoryState(),
      toolStart('t', 'read_file'),
      toolResult('t', '{"status":"success","input_bytes":1,"output_bytes":2,"truncated":false}'),
    )
    expect(state.toolCalls.get('t')).toEqual({
      toolCallId: 't', toolName: 'read_file', status: 'completed',
      summary: '{"status":"success","input_bytes":1,"output_bytes":2,"truncated":false}',
      startedAt: now(), endedAt: now(),
    })
    expect(state.timeline.map((item) => item.type)).toEqual(['tool'])
  })

  it('marks failed when the summary carries error fields', () => {
    const state = apply(
      createAgentHistoryState(),
      toolStart('t', 'write_file'),
      toolResult('t', '{"status":"error","error_code":"E_READ","error_message":"gone"}'),
    )
    expect(state.toolCalls.get('t')?.status).toBe('failed')
  })

  it('degrades non-JSON summaries to plain text with completed status', () => {
    const state = apply(createAgentHistoryState(), toolStart('t', 'x'), toolResult('t', 'plain text summary'))
    expect(state.toolCalls.get('t')).toMatchObject({ status: 'completed', summary: 'plain text summary' })
  })

  it('backfills a RESULT without START and ignores repeated RESULTs', () => {
    const state = apply(createAgentHistoryState(), toolResult('t', '{}'), toolResult('t', '{"error_code":"E"}'))
    expect(state.toolCalls.get('t')?.status).toBe('completed')
    expect(state.toolCalls.get('t')?.summary).toBe('{}')
    expect(state.timeline).toEqual([{ type: 'tool', refId: 't', at: now() }])
    const finished = apply(createAgentHistoryState(), toolStart('t', 'x'), toolResult('t', '{}'))
    expect(applyAguiEvent(finished, toolResult('t', '{"error_code":"E"}'), now)).toEqual(finished)
  })
})

describe('applyAguiEvent runs', () => {
  it('tracks active -> finished', () => {
    const state = apply(createAgentHistoryState(), runStarted('r'), runFinished('r'))
    expect(state.runs.get('r')).toEqual({
      runId: 'r', status: 'finished', errorCode: null, errorMessage: null, startedAt: now(), endedAt: now(),
    })
  })

  it('applies RUN_ERROR to the most recently started active run', () => {
    const state = apply(createAgentHistoryState(), runStarted('r'), runError('boom', 'E_RUN'))
    expect(state.runs.get('r')).toMatchObject({ status: 'error', errorCode: 'E_RUN', errorMessage: 'boom', endedAt: now() })
  })

  it('drops RUN_ERROR when no run is active (no runId on the wire)', () => {
    const state = apply(createAgentHistoryState(), runError('boom'))
    expect(state.runs.size).toBe(0)
    expect(state.timeline).toEqual([])
  })

  it('backfills unknown RUN_FINISHED ids and ignores terminal repeats', () => {
    const state = apply(createAgentHistoryState(), runFinished('ghost'))
    expect(state.runs.get('ghost')).toMatchObject({ status: 'finished', startedAt: now(), endedAt: now() })
    const started = apply(createAgentHistoryState(), runStarted('r'))
    const finished = apply(started, runFinished('r'))
    expect(applyAguiEvent(finished, runFinished('r'), now)).toEqual(finished)
  })

  it('treats a new RUN_STARTED after finish as its own active run', () => {
    const state = apply(createAgentHistoryState(), runStarted('r'), runFinished('r'), runStarted('s'))
    expect(state.runs.get('s')?.status).toBe('active')
    expect(state.runs.get('r')?.status).toBe('finished')
  })
})

describe('applyAguiEvent permissions and backend', () => {
  it('upserts the pending visibility entry and preserves REST-owned state', () => {
    const state = apply(
      createAgentHistoryState(),
      permission('a', { tool_name: 'write_file', evidence: 'e', expires_at: '2026-08-13T13:00:00+00:00' }),
      permission('a', { tool_name: 'renamed' }),
    )
    expect(state.permissions.get('a')).toEqual({
      approvalId: 'a', toolName: 'renamed', evidence: 'e', expiresAt: '2026-08-13T13:00:00+00:00', state: 'pending', decidedAt: null,
    })
    expect(state.timeline).toEqual([{ type: 'permission', refId: 'a', at: now() }])
  })

  it('ignores other CUSTOM names and malformed approval ids', () => {
    const other: AguiEvent = { type: 'CUSTOM', name: 'something.else', value: { x: 1 } }
    const noId: AguiEvent = { type: 'CUSTOM', name: 'termflow.permission_requested', value: { tool_name: 'x' } }
    const state = apply(createAgentHistoryState(), other, noId)
    expect(state.permissions.size).toBe(0)
    expect(state.timeline).toEqual([])
  })

  it('overwrites backend state/epoch on /backend STATE_DELTA and ignores other paths', () => {
    const state = apply(
      createAgentHistoryState(),
      stateDelta('running', 7),
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/other', value: { state: 'x', epoch: 1 } }] },
      { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/backend', value: { state: 'closed', epoch: 9 } }] },
    )
    expect(state.backend).toEqual({ state: 'closed', epoch: 9 })
  })

  it('keeps backend null on missing epoch or unknown state values', () => {
    const noEpoch = apply(createAgentHistoryState(), {
      type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/backend', value: { state: 'ready' } }],
    })
    expect(noEpoch.backend).toEqual({ state: 'ready', epoch: null })
    const badState = apply(createAgentHistoryState(), {
      type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/backend', value: { state: '', epoch: 3 } }],
    })
    expect(badState.backend).toEqual({ state: null, epoch: null })
  })
})

describe('applyAguiEvent purity and idempotency', () => {
  it('does not mutate the input state and repeats converge', () => {
    const initial = createAgentHistoryState()
    const events: AguiEvent[] = [
      chunk('m', 'a'), chunk('m', 'b'), end('m'), end('m'), chunk('m', 'late'),
      toolStart('t', 'x'), toolResult('t', '{}'), toolResult('t', '{"error_code":"E"}'),
      runStarted('r'), runFinished('r'), runFinished('r'),
      permission('a'), permission('a'),
      stateDelta('ready', 3),
    ]
    const once = apply(initial, ...events)
    const twice = apply(once, ...events)
    expect(initial.messages.size).toBe(0)
    expect(twice).toEqual(once)
  })

  it('preserves timeline arrival order', () => {
    const state = apply(
      createAgentHistoryState(),
      chunk('m', 'x'), toolStart('t', 'n'), permission('a'), runStarted('r'),
    )
    expect(state.timeline.map((item) => item.type)).toEqual(['message', 'tool', 'permission', 'run'])
  })
})

describe('seedUserMessages', () => {
  const row = (overrides: Partial<AgentMessageResponse> = {}): AgentMessageResponse => ({
    message_id: '11111111-1111-4111-8111-111111111111',
    conversation_id: '22222222-2222-4222-8222-222222222222',
    run_id: null,
    role: 'user',
    kind: 'user_message',
    assembly_revision: 1,
    is_final: true,
    body_digest: 'd',
    body: 'hello there',
    created_at: '2026-08-12T00:00:00+00:00',
    ...overrides,
  })

  it('injects user rows as accepted echoes with timeline entries', () => {
    const state = seedUserMessages(createAgentHistoryState(), [row()], now)
    const entry = state.userMessages.get('11111111-1111-4111-8111-111111111111')
    expect(entry).toEqual({
      clientId: '11111111-1111-4111-8111-111111111111',
      text: 'hello there',
      deliveryState: 'accepted',
      error: null,
    })
    expect(state.timeline).toHaveLength(1)
    expect(state.timeline[0]?.type).toBe('user')
  })

  it('skips assistant rows (events are the single source for them)', () => {
    const state = seedUserMessages(createAgentHistoryState(), [row({ role: 'assistant' })], now)
    expect(state.userMessages.size).toBe(0)
  })

  it('degrades null body rows to an empty placeholder', () => {
    const state = seedUserMessages(createAgentHistoryState(), [row({ body: null })], now)
    expect(state.userMessages.get('11111111-1111-4111-8111-111111111111')?.text).toBe('')
  })

  it('is idempotent and does not mutate the input', () => {
    const initial = createAgentHistoryState()
    const seeded = seedUserMessages(initial, [row()], now)
    const reseeded = seedUserMessages(seeded, [row()], now)
    expect(initial.userMessages.size).toBe(0)
    expect(reseeded).toEqual(seeded)
  })
})
