import type { AgentEventResponse } from '@termflow/client-contracts'
import type { AgentStreamTransportEvent } from './ports'

//: SSE frame names emitted by the Agent stream endpoint
//: (termflow_control_plane.api.agent_stream).
const SSE_EVENT = 'agent_event'
const SSE_RESET = 'reset'
const SSE_CLOSED = 'closed'

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const nonEmptyString = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string'
const uuid = (value: unknown): value is string =>
  typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
//: The opaque cursor is `{epoch}-{database_seq}`; storage details are never
//: exposed to the client beyond this string.
const opaqueCursor = (value: unknown): value is string =>
  typeof value === 'string' && /^[1-9][0-9]*-[0-9]+$/.test(value)
const nonNegativeInteger = (value: unknown): value is number => Number.isInteger(value) && Number(value) >= 0

function agentEvent(value: unknown): AgentEventResponse | null {
  if (!record(value)) return null
  if (!uuid(value.event_id) || !uuid(value.conversation_id)) return null
  if (!nullableString(value.run_id) || !nonEmptyString(value.event_kind)) return null
  if (!nonNegativeInteger(value.database_seq) || !nonEmptyString(value.payload_digest)) return null
  if (typeof value.ephemeral !== 'boolean' || !nonEmptyString(value.created_at)) return null
  return {
    event_id: value.event_id,
    conversation_id: value.conversation_id,
    run_id: value.run_id,
    event_kind: value.event_kind,
    database_seq: value.database_seq,
    payload_digest: value.payload_digest,
    ephemeral: value.ephemeral,
    created_at: value.created_at,
  }
}

/**
 * Parse one complete SSE frame (``event: <name>`` + ``data: <json>`` lines)
 * delivered by an Agent stream transport.
 *
 * Returns the semantic transport event, or ``null`` for unknown or malformed
 * frames so the session can ignore them without logging untrusted content.
 */
export function parseAgentStreamFrame(frame: string): AgentStreamTransportEvent | null {
  let name: string | null = null
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) name = line.slice('event: '.length)
    else if (line.startsWith('data: ')) data += line.slice('data: '.length)
  }
  if (name === null || data === '') return null

  let payload: unknown
  try {
    payload = JSON.parse(data)
  } catch {
    return null
  }
  if (!record(payload)) return null

  switch (name) {
    case SSE_EVENT: {
      if (!record(payload.event) || !opaqueCursor(payload.cursor)) return null
      const event = agentEvent(payload.event)
      return event === null ? null : { type: 'event', event, cursor: payload.cursor }
    }
    case SSE_RESET:
      // cursor_too_old: the server mints a fresh cursor and never silently
      // skips events, so the client must reload state from REST.
      return opaqueCursor(payload.cursor) ? { type: 'reset', cursor: payload.cursor } : null
    case SSE_CLOSED:
      return Number.isInteger(payload.code) && nonEmptyString(payload.reason)
        ? { type: 'close', code: payload.code as number, reason: payload.reason }
        : null
    default:
      return null
  }
}
