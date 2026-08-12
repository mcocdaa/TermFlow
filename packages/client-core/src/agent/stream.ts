import type { AgentEventResponse } from '@termflow/client-contracts'
import {
  AGENT_STREAM_CLOSE_AUTH_EPOCH,
  AGENT_STREAM_CLOSE_BINDING_REVOKED,
  AGENT_STREAM_CLOSE_TOO_SLOW,
  type AgentStreamConnectRequest,
  type AgentStreamConnection,
  type AgentStreamScheduler,
  type AgentStreamTransport,
  type AgentStreamTransportEvent,
} from './ports'

export type AgentStreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface AgentStreamCloseInfo {
  code: number
  reason: string
}

export interface AgentStreamCallbacks {
  onStatus: (status: AgentStreamStatus) => void
  onEvent: (event: AgentEventResponse) => void
  /**
   * cursor_too_old: the server minted a fresh cursor instead of silently
   * skipping events; the caller must reload state from REST history.
   */
  onReset: (cursor: string) => void
  onClosed: (info: AgentStreamCloseInfo) => void
  onError: (error: { code: string, message?: string }) => void
  onAuthenticationRequired: () => void
}

export interface AgentStreamOptions {
  transport: AgentStreamTransport
  scheduler: AgentStreamScheduler
  /**
   * REST replay used to recover from a slow-consumer disconnect (4410) and
   * from cursor_too_old: pages the conversation's canonical events after
   * `sinceSeq` and resolves them in `database_seq` order.
   */
  replay: (conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]>
  reconnectDelayMs?: number
}

const MAX_RECONNECT_DELAY_MS = 10_000
//: Global live streams carry no sequence component in the opaque cursor, so
//: duplicates are deduplicated by event id over a bounded recent window.
const GLOBAL_DEDUP_WINDOW = 512

/**
 * Conversation-scoped or global Agent live stream client.
 *
 * Mirrors the terminal session's recovery model: subscribe, replay, and
 * resume through the opaque cursor; recover dropped events from REST replay
 * after a slow-consumer disconnect; never silently skip after a
 * `cursor_too_old` reset; surface authentication epoch and binding revocation
 * closures without reconnecting.
 */
export class AgentStreamSession {
  private connection: AgentStreamConnection | null = null
  private reconnectTimer: unknown | null = null
  private connectionGeneration = 0
  private disposed = false
  private suppressReconnect = false
  private reconnectAttempt = 0
  private cursor: string | null = null
  private lastSeq = 0
  private readonly recentEventIds = new Set<string>()
  private readonly reconnectDelayMs: number

  constructor(
    private readonly conversationId: string | null,
    private readonly callbacks: AgentStreamCallbacks,
    private readonly options: AgentStreamOptions,
  ) {
    this.reconnectDelayMs = options.reconnectDelayMs ?? 1_000
  }

  async connect(): Promise<void> {
    if (this.disposed) return
    this.callbacks.onStatus(this.cursor === null ? 'connecting' : 'reconnecting')
    const generation = ++this.connectionGeneration
    const request: AgentStreamConnectRequest = {}
    if (this.conversationId !== null) request.conversationId = this.conversationId
    if (this.cursor !== null) request.cursor = this.cursor
    try {
      const connection = await this.options.transport.connect(request, (event) => {
        if (generation === this.connectionGeneration) this.handleEvent(event)
      })
      if (generation !== this.connectionGeneration || this.disposed) {
        await connection.close(1000, 'stale_connect')
        return
      }
      this.connection = connection
    } catch {
      if (generation === this.connectionGeneration) this.handleClose(1006, 'transport_error')
    }
  }

  private handleEvent(event: AgentStreamTransportEvent): void {
    if (event.type === 'open') {
      this.reconnectAttempt = 0
      this.callbacks.onStatus('connected')
      return
    }
    if (event.type === 'close') {
      this.handleClose(event.code, event.reason)
      return
    }
    if (event.type === 'event') {
      this.deliverLiveEvent(event.event, event.cursor)
      return
    }
    this.handleReset(event.cursor)
  }

  private deliverLiveEvent(event: AgentEventResponse, cursor: string): void {
    if (this.conversationId !== null) {
      // Conversation streams resume by database_seq: an append between the
      // replay query and the subscription (or a reconnect overlap) is
      // delivered exactly once.
      if (event.database_seq <= this.lastSeq) return
      this.lastSeq = event.database_seq
    } else {
      if (this.recentEventIds.has(event.event_id)) return
      this.recentEventIds.add(event.event_id)
      if (this.recentEventIds.size > GLOBAL_DEDUP_WINDOW) this.recentEventIds.clear()
    }
    this.cursor = cursor
    this.callbacks.onEvent(event)
  }

  private handleReset(cursor: string): void {
    // cursor_too_old: continuity cannot be proven. The stream stays open and
    // the server mints a fresh cursor at the stored max; the client reloads
    // the missing range from REST history rather than silently skipping
    // anything. Already-delivered events stay valid, so replay resumes from
    // the last delivered sequence.
    this.cursor = cursor
    this.recentEventIds.clear()
    this.callbacks.onReset(cursor)
    if (this.conversationId === null) return
    void this.runReplay(this.lastSeq)
  }

  private handleClose(code: number, reason: string): void {
    this.connectionGeneration += 1
    this.connection = null
    if (code === AGENT_STREAM_CLOSE_AUTH_EPOCH) {
      // Credential rotation invalidates the stream even while idle.
      this.suppressReconnect = true
      this.callbacks.onStatus('closed')
      this.callbacks.onAuthenticationRequired()
      return
    }
    if (code === AGENT_STREAM_CLOSE_BINDING_REVOKED) {
      // Revocation closes the stream; the conversation is gone.
      this.suppressReconnect = true
      this.callbacks.onStatus('closed')
      this.callbacks.onClosed({ code, reason })
      return
    }
    if (this.disposed || this.suppressReconnect) return
    if (code === AGENT_STREAM_CLOSE_TOO_SLOW) {
      // The live queue dropped events: recover through REST replay, then
      // resume the stream after the replayed range.
      this.callbacks.onError({ code: 'stream_too_slow' })
      if (this.conversationId !== null) {
        void this.recoverThroughReplay()
        return
      }
    }
    this.scheduleReconnect()
  }

  private async recoverThroughReplay(): Promise<void> {
    const replaySince = this.lastSeq
    try {
      const events = await this.options.replay(this.conversationId as string, replaySince)
      if (this.disposed) return
      for (const event of events) this.deliverReplayEvent(event)
    } catch {
      this.callbacks.onError({ code: 'replay_failed' })
    }
    if (this.disposed) return
    this.scheduleReconnect()
  }

  private async runReplay(sinceSeq: number): Promise<void> {
    if (this.disposed || this.conversationId === null) return
    try {
      const events = await this.options.replay(this.conversationId, sinceSeq)
      if (this.disposed) return
      for (const event of events) this.deliverReplayEvent(event)
    } catch {
      this.callbacks.onError({ code: 'replay_failed' })
    }
  }

  private deliverReplayEvent(event: AgentEventResponse): void {
    if (this.conversationId === null) return
    // A live delta may already have delivered the same sequence during the
    // replay; both directions of the race converge on exactly once.
    if (event.database_seq <= this.lastSeq) return
    this.lastSeq = event.database_seq
    this.callbacks.onEvent(event)
  }

  private scheduleReconnect(): void {
    this.callbacks.onStatus('reconnecting')
    const delay = Math.min(MAX_RECONNECT_DELAY_MS, this.reconnectDelayMs * 2 ** this.reconnectAttempt)
    this.reconnectAttempt += 1
    this.reconnectTimer = this.options.scheduler.set(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }

  async dispose(): Promise<void> {
    this.disposed = true
    this.connectionGeneration += 1
    if (this.reconnectTimer !== null) this.options.scheduler.clear(this.reconnectTimer)
    this.reconnectTimer = null
    const connection = this.connection
    this.connection = null
    if (connection !== null) await connection.close(1000, 'client_closed')
  }
}
