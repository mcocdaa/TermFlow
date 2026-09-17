import type { AgentEventResponse } from '@termflow/client-contracts'

//: In-band close codes produced by the Agent stream endpoint
//: (termflow_control_plane.api.agent_stream).
export const AGENT_STREAM_CLOSE_AUTH_EPOCH = 4401
export const AGENT_STREAM_CLOSE_FORBIDDEN = 4403
export const AGENT_STREAM_CLOSE_NOT_FOUND = 4404
export const AGENT_STREAM_CLOSE_TOO_SLOW = 4410
export const AGENT_STREAM_CLOSE_BINDING_REVOKED = 4412

/**
 * Initial HTTP status → terminal close frame, shared by every agent stream
 * transport (browser fetch and the native Tauri command mirror this table,
 * M6b spec §4.2). Only 401 is a session-death signal (4401); 403 and 404
 * are terminal but leave the session intact, and everything else is a
 * transient 1006 so the session may retry with backoff.
 */
export function agentStreamCloseForStatus(status: number): { code: number, reason: string } {
  if (status === 401) return { code: AGENT_STREAM_CLOSE_AUTH_EPOCH, reason: 'authentication_required' }
  if (status === 403) return { code: AGENT_STREAM_CLOSE_FORBIDDEN, reason: 'forbidden' }
  if (status === 404) return { code: AGENT_STREAM_CLOSE_NOT_FOUND, reason: 'conversation_not_found' }
  return { code: 1006, reason: 'http_error' }
}

export interface AgentStreamConnectRequest {
  /** Omit to subscribe to the global live stream (every conversation). */
  conversationId?: string
  /** Opaque `{epoch}-{seq}` cursor from the last delivered frame. */
  cursor?: string
}

/**
 * Semantic transport event emitted by an Agent stream transport. The event
 * payload is generic: canonical transports carry ``AgentEventResponse``
 * (default), agui transports carry hand-written AG-UI events (M6b spec
 * §4.3). ``reset``/``closed`` are wire-independent B semantics.
 */
export type AgentStreamTransportEvent<TEvent = AgentEventResponse> =
  | { type: 'open' }
  | { type: 'event', event: TEvent, cursor: string }
  | { type: 'reset', cursor: string }
  | { type: 'close', code: number, reason: string }

export interface AgentStreamConnection {
  close(code: number, reason: string): Promise<void>
}

export interface AgentStreamTransport<TEvent = AgentEventResponse> {
  connect(
    request: AgentStreamConnectRequest,
    emit: (event: AgentStreamTransportEvent<TEvent>) => void,
  ): Promise<AgentStreamConnection>
}

export interface AgentStreamScheduler {
  set(callback: () => void | Promise<void>, delayMs: number): unknown
  clear(handle: unknown): void
}
