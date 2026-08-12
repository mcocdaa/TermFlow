import type { AgentEventResponse } from '@termflow/client-contracts'

//: In-band close codes produced by the Agent stream endpoint
//: (termflow_control_plane.api.agent_stream).
export const AGENT_STREAM_CLOSE_AUTH_EPOCH = 4401
export const AGENT_STREAM_CLOSE_TOO_SLOW = 4410
export const AGENT_STREAM_CLOSE_BINDING_REVOKED = 4412

export interface AgentStreamConnectRequest {
  /** Omit to subscribe to the global live stream (every conversation). */
  conversationId?: string
  /** Opaque `{epoch}-{seq}` cursor from the last delivered frame. */
  cursor?: string
}

export type AgentStreamTransportEvent =
  | { type: 'open' }
  | { type: 'event', event: AgentEventResponse, cursor: string }
  | { type: 'reset', cursor: string }
  | { type: 'close', code: number, reason: string }

export interface AgentStreamConnection {
  close(code: number, reason: string): Promise<void>
}

export interface AgentStreamTransport {
  connect(
    request: AgentStreamConnectRequest,
    emit: (event: AgentStreamTransportEvent) => void,
  ): Promise<AgentStreamConnection>
}

export interface AgentStreamScheduler {
  set(callback: () => void | Promise<void>, delayMs: number): unknown
  clear(handle: unknown): void
}
