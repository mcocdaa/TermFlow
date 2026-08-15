//: Hand-written AG-UI 0.1.19 wire event types and structural validator.
//:
//: These mirror the B-side projection pinned by
//: `apps/control-plane/tests/fixtures/agui/agui-0.1.19-wire.json` (M6a spec
//: §4.3): uppercase snake ``type`` discriminators, camelCase field names,
//: optional millisecond ``timestamp``. They are intentionally NOT part of
//: generated.ts: the AG-UI wire is a pinned external contract, not a B
//: Pydantic model surface (M6a spec §4.7), and the AG-UI package itself is
//: never imported (wire projection only).

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const nonEmptyString = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const finiteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

export interface AguiRunStartedEvent {
  type: 'RUN_STARTED'
  threadId: string
  runId: string
  timestamp?: number
}

export interface AguiTextMessageChunkEvent {
  type: 'TEXT_MESSAGE_CHUNK'
  messageId: string
  /** B always projects ``assistant``; kept tolerant to wire drift. */
  role?: string
  delta: string
  timestamp?: number
}

export interface AguiTextMessageEndEvent {
  type: 'TEXT_MESSAGE_END'
  messageId: string
  timestamp?: number
}

export interface AguiToolCallStartEvent {
  type: 'TOOL_CALL_START'
  toolCallId: string
  toolCallName: string
  timestamp?: number
}

export interface AguiToolCallResultEvent {
  type: 'TOOL_CALL_RESULT'
  /** Synthesized from ``toolCallId`` by the B projection (M6a §4.3). */
  messageId: string
  toolCallId: string
  /** Bounded summary JSON string; render as plain text. */
  content: string
  role?: string
  timestamp?: number
}

export interface AguiCustomEvent {
  type: 'CUSTOM'
  name: string
  value: Record<string, unknown>
  timestamp?: number
}

export interface AguiRunFinishedEvent {
  type: 'RUN_FINISHED'
  threadId: string
  runId: string
  timestamp?: number
}

export interface AguiRunErrorEvent {
  type: 'RUN_ERROR'
  message: string
  code?: string
  timestamp?: number
}

export interface AguiStateDeltaEvent {
  type: 'STATE_DELTA'
  /** RFC 6902 patch array; B only emits ``/backend`` replace ops (M6a §4.3). */
  delta: Record<string, unknown>[]
  timestamp?: number
}

export type AguiEvent =
  | AguiRunStartedEvent
  | AguiTextMessageChunkEvent
  | AguiTextMessageEndEvent
  | AguiToolCallStartEvent
  | AguiToolCallResultEvent
  | AguiCustomEvent
  | AguiRunFinishedEvent
  | AguiRunErrorEvent
  | AguiStateDeltaEvent

/** One batch of REST-replayed AG-UI events plus its seq watermark (M6b §4.3). */
export interface AguiReplayBatch {
  events: AguiEvent[]
  /** database_seq covered by this batch; the session advances ``lastSeq``. */
  coveredThrough: number
}

function withTimestamp<T extends { timestamp?: number }>(
  out: Omit<T, 'timestamp'>,
  value: Record<string, unknown>,
): T | null {
  if (value.timestamp !== undefined && !finiteNumber(value.timestamp)) return null
  const event = out as T
  if (value.timestamp !== undefined) event.timestamp = value.timestamp
  return event
}

function parseRunStarted(value: Record<string, unknown>): AguiRunStartedEvent | null {
  if (!nonEmptyString(value.threadId) || !nonEmptyString(value.runId)) return null
  return withTimestamp({ type: 'RUN_STARTED', threadId: value.threadId, runId: value.runId }, value)
}

function parseTextMessageChunk(value: Record<string, unknown>): AguiTextMessageChunkEvent | null {
  if (!nonEmptyString(value.messageId) || !nonEmptyString(value.delta)) return null
  if (value.role !== undefined && !nonEmptyString(value.role)) return null
  const event: AguiTextMessageChunkEvent = { type: 'TEXT_MESSAGE_CHUNK', messageId: value.messageId, delta: value.delta }
  if (typeof value.role === 'string') event.role = value.role
  return withTimestamp(event, value)
}

function parseTextMessageEnd(value: Record<string, unknown>): AguiTextMessageEndEvent | null {
  if (!nonEmptyString(value.messageId)) return null
  return withTimestamp({ type: 'TEXT_MESSAGE_END', messageId: value.messageId }, value)
}

function parseToolCallStart(value: Record<string, unknown>): AguiToolCallStartEvent | null {
  if (!nonEmptyString(value.toolCallId) || !nonEmptyString(value.toolCallName)) return null
  return withTimestamp({ type: 'TOOL_CALL_START', toolCallId: value.toolCallId, toolCallName: value.toolCallName }, value)
}

function parseToolCallResult(value: Record<string, unknown>): AguiToolCallResultEvent | null {
  if (!nonEmptyString(value.messageId) || !nonEmptyString(value.toolCallId) || typeof value.content !== 'string') return null
  if (value.role !== undefined && !nonEmptyString(value.role)) return null
  const event: AguiToolCallResultEvent = { type: 'TOOL_CALL_RESULT', messageId: value.messageId, toolCallId: value.toolCallId, content: value.content }
  if (typeof value.role === 'string') event.role = value.role
  return withTimestamp(event, value)
}

function parseCustom(value: Record<string, unknown>): AguiCustomEvent | null {
  if (!nonEmptyString(value.name) || !record(value.value)) return null
  return withTimestamp({ type: 'CUSTOM', name: value.name, value: value.value }, value)
}

function parseRunFinished(value: Record<string, unknown>): AguiRunFinishedEvent | null {
  if (!nonEmptyString(value.threadId) || !nonEmptyString(value.runId)) return null
  return withTimestamp({ type: 'RUN_FINISHED', threadId: value.threadId, runId: value.runId }, value)
}

function parseRunError(value: Record<string, unknown>): AguiRunErrorEvent | null {
  if (!nonEmptyString(value.message)) return null
  if (value.code !== undefined && !nonEmptyString(value.code)) return null
  const event: AguiRunErrorEvent = { type: 'RUN_ERROR', message: value.message }
  if (typeof value.code === 'string') event.code = value.code
  return withTimestamp(event, value)
}

function parseStateDelta(value: Record<string, unknown>): AguiStateDeltaEvent | null {
  if (!Array.isArray(value.delta) || value.delta.length === 0) return null
  if (!value.delta.every((operation) => record(operation)
    && nonEmptyString(operation.op) && nonEmptyString(operation.path))) return null
  return withTimestamp({ type: 'STATE_DELTA', delta: value.delta as Record<string, unknown>[] }, value)
}

const PARSERS: Record<string, (value: Record<string, unknown>) => AguiEvent | null> = {
  RUN_STARTED: parseRunStarted,
  TEXT_MESSAGE_CHUNK: parseTextMessageChunk,
  TEXT_MESSAGE_END: parseTextMessageEnd,
  TOOL_CALL_START: parseToolCallStart,
  TOOL_CALL_RESULT: parseToolCallResult,
  CUSTOM: parseCustom,
  RUN_FINISHED: parseRunFinished,
  RUN_ERROR: parseRunError,
  STATE_DELTA: parseStateDelta,
}

/**
 * Structurally validate one AG-UI wire event object.
 *
 * Returns the typed event for the nine projected kinds, or ``null`` for
 * unknown types and malformed shapes. Tolerant by design (M6b spec §10 risk
 * 1): the AG-UI wire can drift, so validation never throws — malformed or
 * unknown events are simply discarded by the caller.
 */
export function parseAguiEvent(value: unknown): AguiEvent | null {
  try {
    if (!record(value)) return null
    if (typeof value.type !== 'string') return null
    const parser = PARSERS[value.type]
    return parser === undefined ? null : parser(value)
  } catch {
    // Hostile property access (throwing getters etc.) must never crash a
    // wire validator: degrade to a dropped event.
    return null
  }
}
