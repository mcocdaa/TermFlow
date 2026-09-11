import { describe, expect, it } from 'vitest'
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
const permission = (approvalId: string): AguiEvent => ({
  type: 'CUSTOM',
  name: 'termflow.permission_requested',
  value: { approval_request_id: approvalId },
})
const stateDelta = (state: string, epoch: number): AguiEvent => ({
  type: 'STATE_DELTA',
  delta: [{ op: 'replace', path: '/backend', value: { state, epoch } }],
})

const apply = (state: AgentHistoryState, ...events: AguiEvent[]) =>
  events.reduce((current, event) => applyAguiEvent(current, event, now), state)

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
})

describe('seedUserMessages', () => {
  it('keeps a server-provided system message as a collapsed-ready timeline item', () => {
    const next = seedUserMessages(createAgentHistoryState(), [
      {
        message_id: 'system-1', conversation_id: 'c', run_id: null, role: 'system', kind: 'prompt',
        assembly_revision: 1, is_final: true, body_digest: 'digest', body: 'system prompt', created_at: '2026-01-01T00:00:00Z',
      },
    ] as never, now)
    expect(next.messages.get('system-1')).toMatchObject({ role: 'system', text: 'system prompt', status: 'complete' })
    expect(next.timeline).toContainEqual({ type: 'message', refId: 'system-1', at: Date.parse('2026-01-01T00:00:00Z') })
  })
})

describe('backend state convergence', () => {
  it('returns an in-flight busy state to ready when the run finishes', () => {
    let state = createAgentHistoryState()
    state = apply(state, runStarted('r'), stateDelta('busy', 1), runFinished('r'))
    expect(state.backend.state).toBe('ready')
  })

  it('keeps an explicit terminal state when the run finishes', () => {
    let state = createAgentHistoryState()
    state = apply(state, runStarted('r'), stateDelta('context_lost', 1), runFinished('r'))
    expect(state.backend.state).toBe('context_lost')
  })
})

describe('turn completion state convergence', () => {
  it('returns busy to ready when a finished text has no tool running', () => {
    let state = createAgentHistoryState()
    state = apply(
      state,
      stateDelta('busy', 1),
      chunk('m1', '收到', 10),
      end('m1'),
    )
    expect(state.backend.state).toBe('ready')
  })

  it('stays busy while a tool call is still running', () => {
    let state = createAgentHistoryState()
    state = apply(
      state,
      stateDelta('busy', 1),
      chunk('m1', 'let me check', 10),
      toolStart('t1', 'pane_read'),
      end('m1'),
    )
    expect(state.backend.state).toBe('busy')
  })
})

describe('thinking frames', () => {
  it('accumulates reasoning and completes on the completed frame', () => {
    let state = createAgentHistoryState()
    state = apply(state, { type: 'CUSTOM', name: 'termflow.thinking_delta', value: { id: 'th1', delta: '分析' } } as AguiEvent)
    state = apply(state, { type: 'CUSTOM', name: 'termflow.thinking_delta', value: { id: 'th1', delta: '中' } } as AguiEvent)
    state = apply(state, { type: 'CUSTOM', name: 'termflow.thinking_completed', value: { id: 'th1' } } as AguiEvent)
    expect(state.thinking.get('th1')).toMatchObject({ text: '分析中', status: 'complete' })
    expect(state.timeline.some((item) => item.type === 'thinking')).toBe(true)
  })
})
