import { describe, expect, it } from 'vitest'
import { applyAguiEvent, createAgentHistoryState, type AgentHistoryState } from './history'
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
