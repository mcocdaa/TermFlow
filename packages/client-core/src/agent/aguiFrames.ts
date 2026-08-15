import { parseAguiEvent, type AguiEvent } from './agui'
import type { AgentStreamTransportEvent } from './ports'
import { isValidAgentCursor } from './cursorStore'

//: SSE frame names emitted by the Agent stream endpoint
//: (termflow_control_plane.api.agent_stream); identical for both wires.
const SSE_EVENT = 'agent_event'
const SSE_RESET = 'reset'
const SSE_CLOSED = 'closed'

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const nonEmptyString = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const opaqueCursor = (value: unknown): value is string => typeof value === 'string' && isValidAgentCursor(value)

/**
 * Parse one complete SSE frame from an ``?wire=agui`` Agent stream.
 *
 * The envelope logic mirrors the canonical parser in frames.ts (three frame
 * names, opaque cursor validation, ``reset``/``closed`` B semantics — M6a
 * spec §4.4): only the ``agent_event`` payload validation differs, routing
 * the projected event object through ``parseAguiEvent``. frames.ts itself
 * stays byte-for-byte unchanged (M6b spec §4.3).
 *
 * Returns the semantic transport event, or ``null`` for unknown or malformed
 * frames so the session can ignore them without logging untrusted content.
 */
export function parseAgentStreamFrameAgui(frame: string): AgentStreamTransportEvent<AguiEvent> | null {
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
      const event = parseAguiEvent(payload.event)
      return event === null ? null : { type: 'event', event, cursor: payload.cursor }
    }
    case SSE_RESET:
      return opaqueCursor(payload.cursor) ? { type: 'reset', cursor: payload.cursor } : null
    case SSE_CLOSED:
      return Number.isInteger(payload.code) && nonEmptyString(payload.reason)
        ? { type: 'close', code: payload.code as number, reason: payload.reason }
        : null
    default:
      return null
  }
}
