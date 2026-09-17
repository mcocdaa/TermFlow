import type { AgentEventResponse } from '@termflow/client-contracts'
import type { AguiEvent, AguiReplayBatch } from './agui'
import { isValidAgentCursor, parseCursorSeq } from './cursorStore'
import {
  AGENT_STREAM_CLOSE_AUTH_EPOCH,
  AGENT_STREAM_CLOSE_BINDING_REVOKED,
  AGENT_STREAM_CLOSE_FORBIDDEN,
  AGENT_STREAM_CLOSE_NOT_FOUND,
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

export interface AgentStreamCallbacks<TEvent = AgentEventResponse> {
  onStatus: (status: AgentStreamStatus) => void
  onEvent: (event: TEvent) => void
  /**
   * cursor_too_old: the server minted a fresh cursor instead of silently
   * skipping events; the caller must reload state from REST history. Also
   * fired after a global-stream 4410 (stream_too_slow) close, where the
   * dropped live range cannot be recovered through replay: the cursor is
   * the last delivered position ('' when nothing was delivered yet).
   */
  onReset: (cursor: string) => void
  onClosed: (info: AgentStreamCloseInfo) => void
  onError: (error: { code: string, message?: string }) => void
  onAuthenticationRequired: () => void
}

/**
 * Wire-discriminated options: canonical sessions supply ``replay`` (per-event
 * ``database_seq`` dedup, unchanged behaviour); agui sessions supply
 * ``replayAgui`` (batch watermark replay, M6b spec §4.3) and may set
 * ``seedFromSeq`` for the cold-start seed (§4.4).
 */
export type AgentStreamOptions<TEvent = AgentEventResponse> = {
  transport: AgentStreamTransport<TEvent>
  scheduler: AgentStreamScheduler
  /**
   * Opaque cursor restored from persistence (hot recovery, M6b spec §4.4):
   * the session subscribes with it directly and skips the REST seed. Invalid
   * cursors are treated as a cold start — persistence is an optimization,
   * never a correctness dependency.
   */
  initialCursor?: string
  reconnectDelayMs?: number
} & (
  | { replay: (conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]> }
  | {
      replayAgui: (conversationId: string, sinceSeq: number) => Promise<AguiReplayBatch>
      /**
       * Cold start: after the first ``open`` the session seeds history with
       * one batch replay from this seq while live frames are buffered and
       * merged by watermark (M6b spec §4.4). Agui mode only.
       */
      seedFromSeq?: number
    }
  )

const MAX_RECONNECT_DELAY_MS = 10_000
//: Global live streams carry no sequence component in the opaque cursor, so
//: duplicates are deduplicated by event id over a bounded recent-id set.
const GLOBAL_DEDUP_LIMIT = 512

/**
 * Conversation-scoped or global Agent live stream client, wire-generic.
 *
 * Mirrors the terminal session's recovery model: subscribe, replay, and
 * resume through the opaque cursor; recover dropped events from REST replay
 * after a slow-consumer disconnect; never silently skip after a
 * ``cursor_too_old`` reset; surface authentication epoch and binding
 * revocation closures without reconnecting.
 *
 * Canonical mode keeps the M6.2 behaviour exactly (per-event ``database_seq``
 * dedup plus an envelope-cursor cross-check). Agui mode sequences purely off
 * the envelope cursor — AG-UI event objects carry no seq — and replays in
 * batches: live frames arriving during a replay are buffered, then flushed
 * by watermark (drop ``seq <= coveredThrough``, deliver the rest in order).
 */
export class AgentStreamSession<TEvent = AgentEventResponse> {
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
  private readonly aguiMode: boolean
  private readonly replayFn: ((conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]>) | null
  private readonly replayAguiFn: ((conversationId: string, sinceSeq: number) => Promise<AguiReplayBatch>) | null
  private readonly seedFromSeq: number | undefined
  private replayBuffer: Array<{ event: TEvent, cursor: string }> | null = null
  private replayChain: Promise<void> = Promise.resolve()
  private pendingReplays = 0
  private seeded = false

  constructor(
    private readonly conversationId: string | null,
    private readonly callbacks: AgentStreamCallbacks<TEvent>,
    private readonly options: AgentStreamOptions<TEvent>,
  ) {
    this.reconnectDelayMs = options.reconnectDelayMs ?? 1_000
    if ('replayAgui' in options) {
      this.aguiMode = true
      this.replayFn = null
      this.replayAguiFn = options.replayAgui
      this.seedFromSeq = options.seedFromSeq
      if (conversationId === null) {
        // AG-UI events carry no stable id, so a global agui stream cannot be
        // deduplicated (M6b spec §2/§4.3): fail fast instead of misbehaving.
        throw new Error('agui wire requires a conversation-scoped stream')
      }
    } else {
      this.aguiMode = false
      this.replayFn = options.replay
      this.replayAguiFn = null
      this.seedFromSeq = undefined
    }
    const initial = options.initialCursor
    if (initial !== undefined && isValidAgentCursor(initial)) {
      this.cursor = initial
      // The restored cursor implies everything up to its seq is already
      // held: start the watermark there so the server's gap replay overlap
      // is dropped (M6b spec §4.4 hot recovery).
      this.lastSeq = parseCursorSeq(initial) ?? 0
    } else {
      this.cursor = null
    }
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

  private handleEvent(event: AgentStreamTransportEvent<TEvent>): void {
    if (event.type === 'open') {
      this.reconnectAttempt = 0
      this.callbacks.onStatus('connected')
      this.seedAfterOpen()
      return
    }
    if (event.type === 'close') {
      this.handleClose(event.code, event.reason)
      return
    }
    if (event.type === 'event') {
      this.deliverOrBuffer(event.event, event.cursor)
      return
    }
    this.handleReset(event.cursor)
  }

  /** Cold-start seed: one batch replay right after the first open (M6b §4.4). */
  private seedAfterOpen(): void {
    if (this.seeded || !this.aguiMode) return
    // The seed is attempted exactly once; a failed seed surfaces onError and
    // leaves the session live-only (the caller may rebuild the session).
    this.seeded = true
    if (this.seedFromSeq !== undefined) void this.requestBatchReplay(this.seedFromSeq)
  }

  private deliverOrBuffer(event: TEvent, cursor: string): void {
    if (this.replayBuffer !== null) {
      // A batch replay is in flight: live frames wait for the watermark
      // flush, which drops covered seqs and delivers the rest in order.
      this.replayBuffer.push({ event, cursor })
      return
    }
    this.deliverLiveEvent(event, cursor)
  }

  private deliverLiveEvent(event: TEvent, cursor: string): void {
    if (this.conversationId !== null) {
      const seq = parseCursorSeq(cursor)
      if (this.aguiMode) {
        // AG-UI events carry no database_seq: the envelope cursor is the
        // single sequencing source (M6b spec §4.3).
        if (seq === null || seq <= this.lastSeq) return
        this.lastSeq = seq
      } else {
        const canonical = event as AgentEventResponse
        if (canonical.database_seq <= this.lastSeq) return
        // Cross-check: envelope seq and database_seq must agree; a mismatch
        // is a duplicate projection and is dropped (safe direction).
        if (seq === null || seq !== canonical.database_seq) return
        this.lastSeq = canonical.database_seq
      }
    } else {
      // Global streams are canonical-only (agui global throws in the
      // constructor): deduplicate by event id over a bounded set.
      const canonical = event as AgentEventResponse
      if (this.recentEventIds.has(canonical.event_id)) return
      this.recentEventIds.add(canonical.event_id)
      if (this.recentEventIds.size > GLOBAL_DEDUP_LIMIT) this.recentEventIds.clear()
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
    if (this.aguiMode) void this.requestBatchReplay(this.lastSeq)
    else void this.runReplay(this.lastSeq)
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
    if (
      code === AGENT_STREAM_CLOSE_BINDING_REVOKED
      || code === AGENT_STREAM_CLOSE_FORBIDDEN
      || code === AGENT_STREAM_CLOSE_NOT_FOUND
    ) {
      // Terminal closures that do not invalidate the session: the binding
      // was revoked, the credential lacks access, or the conversation is
      // gone. The host surfaces the reason and clears a persisted cursor
      // only when the entity no longer exists.
      this.suppressReconnect = true
      this.callbacks.onStatus('closed')
      this.callbacks.onClosed({ code, reason })
      return
    }
    if (this.disposed || this.suppressReconnect) return
    if (code === AGENT_STREAM_CLOSE_TOO_SLOW) {
      // The live queue dropped events: conversation streams recover through
      // REST replay, then resume the stream after the replayed range.
      this.callbacks.onError({ code: 'stream_too_slow' })
      if (this.conversationId !== null) {
        void this.recoverThroughReplay()
        return
      }
      // Global streams have no replay path, so the dropped range is
      // unrecoverable: clear the dedup window and surface onReset so the
      // consumer knows continuity was lost and reloads state from REST
      // history instead of silently missing events.
      this.recentEventIds.clear()
      this.callbacks.onReset(this.cursor ?? '')
    }
    this.scheduleReconnect()
  }

  private async recoverThroughReplay(): Promise<void> {
    const replaySince = this.lastSeq
    if (this.aguiMode) {
      // Batch watermark replay; the reconnect then resumes with the old
      // opaque cursor (epoch prefix known) and the server's overlap range is
      // dropped by the advanced watermark — REST content is never repeated
      // (M6b spec §4.4).
      await this.requestBatchReplay(replaySince)
    } else {
      try {
        const events = await (this.replayFn as (conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]>)(this.conversationId as string, replaySince)
        if (this.disposed) return
        for (const event of events) this.deliverReplayEvent(event)
      } catch {
        this.callbacks.onError({ code: 'replay_failed' })
      }
    }
    if (this.disposed) return
    this.scheduleReconnect()
  }

  private async runReplay(sinceSeq: number): Promise<void> {
    if (this.disposed || this.conversationId === null) return
    try {
      const events = await (this.replayFn as (conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]>)(this.conversationId, sinceSeq)
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
    this.callbacks.onEvent(event as TEvent)
  }

  /**
   * Queue one batch watermark replay. Replays are serialized and their
   * ``sinceSeq`` is clamped to the current watermark at execution time, so a
   * replay triggered while another is in flight never re-covers an already
   * delivered range. Live frames are buffered for the whole chain and flushed
   * once the last replay finishes (M6b spec §4.3).
   */
  private requestBatchReplay(sinceSeq: number): Promise<void> {
    if (this.disposed) return this.replayChain
    const conversationId = this.conversationId as string
    if (this.replayBuffer === null) this.replayBuffer = []
    this.pendingReplays += 1
    this.replayChain = this.replayChain.then(async () => {
      if (this.disposed) return
      const effective = Math.max(sinceSeq, this.lastSeq)
      try {
        const batch = await (this.replayAguiFn as (conversationId: string, sinceSeq: number) => Promise<AguiReplayBatch>)(conversationId, effective)
        if (!this.disposed) {
          if (batch.coveredThrough > this.lastSeq) this.lastSeq = batch.coveredThrough
          for (const event of batch.events) this.callbacks.onEvent(event as TEvent)
        }
      } catch {
        if (!this.disposed) this.callbacks.onError({ code: 'replay_failed' })
      }
      this.pendingReplays -= 1
      if (this.pendingReplays === 0) {
        try {
          this.flushReplayBuffer()
        } catch {
          // A throwing consumer callback must not poison the replay chain.
        }
      }
    })
    return this.replayChain
  }

  private flushReplayBuffer(): void {
    const buffered = this.replayBuffer
    if (buffered === null) return
    this.replayBuffer = null
    if (this.disposed) return
    for (const frame of buffered) this.deliverLiveEvent(frame.event, frame.cursor)
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
    this.replayBuffer = null
    if (this.reconnectTimer !== null) this.options.scheduler.clear(this.reconnectTimer)
    this.reconnectTimer = null
    const connection = this.connection
    this.connection = null
    if (connection !== null) await connection.close(1000, 'client_closed')
  }
}
