import { describe, expect, it, vi, type Mock } from 'vitest'
import type { AgentEventResponse } from '@termflow/client-contracts'
import type { AguiEvent, AguiReplayBatch } from './agui'
import { AgentStreamSession, type AgentStreamCallbacks } from './stream'
import type {
  AgentStreamConnectRequest,
  AgentStreamConnection,
  AgentStreamTransport,
  AgentStreamTransportEvent,
} from './ports'

const CONVERSATION = '11111111-1111-4111-8111-111111111111'
const EVENT_ID = (n: number) => `22222222-2222-4222-8222-${String(n).padStart(12, '0')}`

function event(seq: number, conversationId = CONVERSATION): AgentEventResponse {
  return {
    event_id: EVENT_ID(seq),
    conversation_id: conversationId,
    run_id: null,
    event_kind: 'message_delta',
    database_seq: seq,
    payload_digest: `digest-${seq}`,
    ephemeral: false,
    created_at: '2026-08-12T00:00:00+00:00',
  }
}

class FakeConnection implements AgentStreamConnection {
  readonly closes: Array<{ code: number, reason: string }> = []
  async close(code: number, reason: string) { this.closes.push({ code, reason }) }
}

class FakeTransport implements AgentStreamTransport {
  readonly requests: AgentStreamConnectRequest[] = []
  readonly connections: FakeConnection[] = []
  readonly emitters: Array<(event: AgentStreamTransportEvent) => void> = []
  async connect(request: AgentStreamConnectRequest, emit: (event: AgentStreamTransportEvent) => void): Promise<AgentStreamConnection> {
    const connection = new FakeConnection()
    this.requests.push(request)
    this.connections.push(connection)
    this.emitters.push(emit)
    return connection
  }
  emit(event: AgentStreamTransportEvent, index = this.emitters.length - 1) { this.emitters[index]?.(event) }
}

class FakeScheduler {
  readonly pending: Array<{ callback: () => void, delay: number, cancelled: boolean }> = []
  set(callback: () => void, delay: number) {
    const handle = { callback, delay, cancelled: false }
    this.pending.push(handle)
    return handle
  }
  clear(handle: { cancelled: boolean }) { handle.cancelled = true }
  runNext() {
    const handle = this.pending.find((candidate) => !candidate.cancelled)
    if (handle) { handle.cancelled = true; handle.callback() }
  }
}

type CallbackSpies = AgentStreamCallbacks & { onEvent: Mock<(event: AgentEventResponse) => void> }

function callbackSpies(): CallbackSpies {
  return {
    onStatus: vi.fn(), onEvent: vi.fn(), onReset: vi.fn(), onClosed: vi.fn(), onError: vi.fn(), onAuthenticationRequired: vi.fn(),
  }
}

async function setup(overrides: { conversationId?: string | null, replay?: (conversationId: string, sinceSeq: number) => Promise<AgentEventResponse[]> } = {}) {
  const transport = new FakeTransport()
  const scheduler = new FakeScheduler()
  const callbacks = callbackSpies()
  const replay = overrides.replay ?? vi.fn(async () => [])
  const session = new AgentStreamSession(
    overrides.conversationId === undefined ? CONVERSATION : overrides.conversationId,
    callbacks,
    {
      transport,
      scheduler,
      replay,
      reconnectDelayMs: 25,
    },
  )
  await session.connect()
  return { session, transport, scheduler, callbacks, replay }
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

function deliveredSeqs(callbacks: CallbackSpies): number[] {
  return callbacks.onEvent.mock.calls.map(([ev]) => ev.database_seq)
}

describe('AgentStreamSession', () => {
  it('subscribes and delivers live events with status transitions', async () => {
    const { transport, callbacks } = await setup()
    expect(transport.requests[0]).toEqual({ conversationId: CONVERSATION })
    expect(callbacks.onStatus).toHaveBeenCalledWith('connecting')
    transport.emit({ type: 'open' })
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('connected')
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    expect(callbacks.onEvent).toHaveBeenCalledWith(event(1))
  })

  it('closes a connection that resolves after its close event', async () => {
    const connection = new FakeConnection()
    let resolveConnection!: (value: AgentStreamConnection) => void
    let emit!: (event: AgentStreamTransportEvent) => void
    const transport: AgentStreamTransport = {
      connect: vi.fn((_request: AgentStreamConnectRequest, callback: (event: AgentStreamTransportEvent) => void) => {
        emit = callback
        return new Promise<AgentStreamConnection>((resolve) => { resolveConnection = resolve })
      }),
    }
    const scheduler = new FakeScheduler()
    const session = new AgentStreamSession(CONVERSATION, callbackSpies(), {
      transport,
      scheduler,
      replay: vi.fn(async () => []),
    })
    const pending = session.connect()
    emit({ type: 'close', code: 1006, reason: 'network' })
    resolveConnection(connection)
    await pending

    expect(connection.closes).toContainEqual({ code: 1000, reason: 'stale_connect' })
    expect(scheduler.pending).toHaveLength(1)
    await session.dispose()
  })

  it('deduplicates by database_seq within a conversation stream', async () => {
    const { transport, callbacks } = await setup()
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: event(3), cursor: '7-3' })
    expect(callbacks.onEvent).toHaveBeenCalledTimes(2)
    expect(deliveredSeqs(callbacks)).toEqual([2, 3])
  })

  it('resumes the stream with the opaque cursor after a network drop and backs off exponentially', async () => {
    const { session, transport, scheduler } = await setup()
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    expect(scheduler.pending[0]?.delay).toBe(25)
    scheduler.runNext()
    await tick()
    expect(transport.requests[1]).toEqual({ conversationId: CONVERSATION, cursor: '7-2' })
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    expect(scheduler.pending[1]?.delay).toBe(50)
    await session.dispose()
  })

  it('caps the reconnect delay at ten seconds', async () => {
    const transport = new FakeTransport()
    const scheduler = new FakeScheduler()
    const session = new AgentStreamSession(CONVERSATION, callbackSpies(), {
      transport,
      scheduler,
      replay: vi.fn(async () => []),
      reconnectDelayMs: 8_000,
    })
    await session.connect()
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    expect(scheduler.pending[0]?.delay).toBe(8_000)
    scheduler.runNext()
    await tick()
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    expect(scheduler.pending[1]?.delay).toBe(10_000)
    await session.dispose()
  })

  it('replays replayed and live overlap exactly once across reconnect', async () => {
    const { session, transport, scheduler, callbacks } = await setup()
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    scheduler.runNext()
    await tick()
    // The server replays from the resumed cursor; the already-delivered
    // sequences must not surface again.
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: event(3), cursor: '7-3' })
    expect(deliveredSeqs(callbacks)).toEqual([1, 2, 3])
    await session.dispose()
  })

  it('surfaces cursor_too_old as onReset and reloads the missing range from REST replay', async () => {
    const { transport, callbacks, replay } = await setup({
      replay: vi.fn(async (_conversationId: string, sinceSeq: number) => [event(1), event(2), event(3)].filter((ev) => ev.database_seq > sinceSeq)),
    })
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'reset', cursor: '7-3' })
    await tick()
    expect(callbacks.onReset).toHaveBeenCalledWith('7-3')
    expect(replay).toHaveBeenCalledWith(CONVERSATION, 1)
    expect(deliveredSeqs(callbacks)).toEqual([1, 2, 3])
    // Live deltas after the reset keep flowing and dedupe against replay.
    transport.emit({ type: 'event', event: event(3), cursor: '7-3' })
    transport.emit({ type: 'event', event: event(4), cursor: '7-4' })
    expect(deliveredSeqs(callbacks)).toEqual([1, 2, 3, 4])
  })

  it('recovers a slow-consumer disconnect through REST replay then resumes the stream', async () => {
    const { session, transport, scheduler, callbacks, replay } = await setup({
      replay: vi.fn(async (_conversationId: string, sinceSeq: number) => [event(3), event(4)].filter((ev) => ev.database_seq > sinceSeq)),
    })
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'close', code: 4410, reason: 'stream_too_slow' })
    await tick()
    expect(callbacks.onError).toHaveBeenCalledWith({ code: 'stream_too_slow' })
    expect(replay).toHaveBeenCalledWith(CONVERSATION, 2)
    expect(deliveredSeqs(callbacks)).toEqual([1, 2, 3, 4])
    // Reconnect resumes after the replayed range with the opaque cursor.
    scheduler.runNext()
    await tick()
    expect(transport.requests[1]).toEqual({ conversationId: CONVERSATION, cursor: '7-2' })
    await session.dispose()
  })

  it('still reconnects when the recovery replay fails', async () => {
    const { session, transport, scheduler, callbacks } = await setup({
      replay: vi.fn(async () => { throw new Error('offline') }),
    })
    transport.emit({ type: 'close', code: 4410, reason: 'stream_too_slow' })
    await tick()
    expect(callbacks.onError).toHaveBeenCalledWith({ code: 'replay_failed' })
    scheduler.runNext()
    await tick()
    expect(transport.requests).toHaveLength(2)
    await session.dispose()
  })

  it('closes without reconnecting when the authentication epoch changes', async () => {
    const { session, transport, scheduler, callbacks } = await setup()
    transport.emit({ type: 'close', code: 4401, reason: 'authentication_epoch_changed' })
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('closed')
    expect(callbacks.onAuthenticationRequired).toHaveBeenCalledTimes(1)
    expect(scheduler.pending).toHaveLength(0)
    await session.dispose()
  })

  it('surfaces binding revocation as onClosed without reconnecting', async () => {
    const { session, transport, scheduler, callbacks } = await setup()
    transport.emit({ type: 'close', code: 4412, reason: 'binding_revoked' })
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('closed')
    expect(callbacks.onClosed).toHaveBeenCalledWith({ code: 4412, reason: 'binding_revoked' })
    expect(scheduler.pending).toHaveLength(0)
    await session.dispose()
  })

  it('deduplicates a global stream by event id and never replays through REST', async () => {
    const { session, transport, callbacks, replay } = await setup({ conversationId: null })
    expect(transport.requests[0]).toEqual({})
    transport.emit({ type: 'event', event: event(1, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'), cursor: '7-0' })
    transport.emit({ type: 'event', event: event(1, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'), cursor: '7-0' })
    transport.emit({ type: 'event', event: event(2, 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'), cursor: '7-0' })
    expect(callbacks.onEvent).toHaveBeenCalledTimes(2)
    transport.emit({ type: 'reset', cursor: '7-5' })
    await tick()
    expect(callbacks.onReset).toHaveBeenCalledWith('7-5')
    expect(replay).not.toHaveBeenCalled()
    await session.dispose()
  })

  it('requests an explicit close when disposed', async () => {
    const { session, transport } = await setup()
    transport.emit({ type: 'open' })
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    await session.dispose()
    expect(transport.connections[0]?.closes).toContainEqual({ code: 1000, reason: 'client_closed' })
  })

  it('stops reconnecting when disposed', async () => {
    const { session, transport, scheduler } = await setup()
    transport.emit({ type: 'close', code: 1006, reason: 'network' })
    await session.dispose()
    scheduler.runNext()
    await tick()
    expect(transport.requests).toHaveLength(1)
  })
})

describe('AgentStreamSession agui wire (M6b spec §4.3/§4.4, plan 1019 client-core matrix)', () => {
  const aguiChunk = (n: number): AguiEvent => ({ type: 'TEXT_MESSAGE_CHUNK', messageId: `m-${n}`, delta: `d-${n}` })

  function deferred<T>() {
    let resolve!: (value: T) => void
    const promise = new Promise<T>((res) => { resolve = res })
    return { promise, resolve }
  }

  class GenericTransport<T> implements AgentStreamTransport<T> {
    readonly requests: AgentStreamConnectRequest[] = []
    readonly connections: FakeConnection[] = []
    readonly emitters: Array<(event: AgentStreamTransportEvent<T>) => void> = []
    async connect(request: AgentStreamConnectRequest, emit: (event: AgentStreamTransportEvent<T>) => void): Promise<AgentStreamConnection> {
      const connection = new FakeConnection()
      this.requests.push(request)
      this.connections.push(connection)
      this.emitters.push(emit)
      return connection
    }
    emit(event: AgentStreamTransportEvent<T>, index = this.emitters.length - 1) { this.emitters[index]?.(event) }
  }

  type AguiCallbackSpies = AgentStreamCallbacks<AguiEvent> & { onEvent: Mock<(event: AguiEvent) => void> }

  function aguiSpies(): AguiCallbackSpies {
    return {
      onStatus: vi.fn(), onEvent: vi.fn(), onReset: vi.fn(), onClosed: vi.fn(), onError: vi.fn(), onAuthenticationRequired: vi.fn(),
    }
  }

  async function setupAgui(overrides: {
    seedFromSeq?: number
    initialCursor?: string
    replayAgui?: (conversationId: string, sinceSeq: number) => Promise<AguiReplayBatch>
  } = {}) {
    const transport = new GenericTransport<AguiEvent>()
    const scheduler = new FakeScheduler()
    const callbacks = aguiSpies()
    const replayAgui = overrides.replayAgui ?? vi.fn(async () => ({ events: [], coveredThrough: 0 }))
    const session = new AgentStreamSession<AguiEvent>(
      CONVERSATION,
      callbacks,
      {
        transport,
        scheduler,
        replayAgui,
        reconnectDelayMs: 25,
        ...(overrides.seedFromSeq === undefined ? {} : { seedFromSeq: overrides.seedFromSeq }),
        ...(overrides.initialCursor === undefined ? {} : { initialCursor: overrides.initialCursor }),
      },
    )
    await session.connect()
    return { session, transport, scheduler, callbacks, replayAgui }
  }

  it('deduplicates live agui frames by envelope cursor seq (duplicate deltas)', async () => {
    const { session, transport, callbacks } = await setupAgui()
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: aguiChunk(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: aguiChunk(3), cursor: '7-3' })
    expect(callbacks.onEvent).toHaveBeenCalledTimes(2)
    expect(callbacks.onEvent.mock.calls.map(([ev]) => ev)).toEqual([aguiChunk(2), aguiChunk(3)])
    await session.dispose()
  })

  it('cold start seed: batch replay from 0 with live frames buffered and merged by watermark', async () => {
    const batch = deferred<AguiReplayBatch>()
    const replayAgui = vi.fn(() => batch.promise)
    const { session, transport, callbacks } = await setupAgui({ seedFromSeq: 0, replayAgui })
    transport.emit({ type: 'open' })
    await tick()
    expect(replayAgui).toHaveBeenCalledWith(CONVERSATION, 0)
    // Live frames arriving during the seed replay are buffered, not delivered.
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: aguiChunk(6), cursor: '7-6' })
    expect(callbacks.onEvent).not.toHaveBeenCalled()
    // The batch covers through seq 5: the buffered seq-2 frame is dropped,
    // the seq-6 frame survives and lands after the batch, in order.
    batch.resolve({ events: [aguiChunk(3), aguiChunk(4)], coveredThrough: 5 })
    await tick()
    expect(callbacks.onEvent.mock.calls.map(([ev]) => ev)).toEqual([aguiChunk(3), aguiChunk(4), aguiChunk(6)])
    // Seed happens once; subsequent live keeps flowing normally.
    transport.emit({ type: 'event', event: aguiChunk(7), cursor: '7-7' })
    expect(callbacks.onEvent).toHaveBeenCalledTimes(4)
    expect(replayAgui).toHaveBeenCalledTimes(1)
    await session.dispose()
  })

  it('reset: onReset surfaces and a batch replay advances the watermark from lastSeq', async () => {
    const replayAgui = vi.fn(async (_id: string, sinceSeq: number) => ({ events: [aguiChunk(sinceSeq + 1)], coveredThrough: 9 }))
    const { session, transport, callbacks } = await setupAgui({ replayAgui })
    transport.emit({ type: 'event', event: aguiChunk(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' })
    transport.emit({ type: 'reset', cursor: '7-9' })
    await tick()
    expect(callbacks.onReset).toHaveBeenCalledWith('7-9')
    expect(replayAgui).toHaveBeenCalledWith(CONVERSATION, 2)
    // Live frames covered by the replay watermark are dropped; later ones flow.
    transport.emit({ type: 'event', event: aguiChunk(9), cursor: '7-9' })
    transport.emit({ type: 'event', event: aguiChunk(10), cursor: '7-10' })
    expect(callbacks.onEvent.mock.calls.map(([ev]) => ev)).toEqual([aguiChunk(1), aguiChunk(2), aguiChunk(3), aguiChunk(10)])
    await session.dispose()
  })

  it('slow client (4410): batch recovery then reconnect with the old cursor drops the server overlap', async () => {
    const replayAgui = vi.fn(async () => ({ events: [aguiChunk(3), aguiChunk(4)], coveredThrough: 5 }))
    const { session, transport, scheduler, callbacks } = await setupAgui({ replayAgui })
    transport.emit({ type: 'event', event: aguiChunk(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' })
    transport.emit({ type: 'close', code: 4410, reason: 'stream_too_slow' })
    await tick()
    expect(callbacks.onError).toHaveBeenCalledWith({ code: 'stream_too_slow' })
    expect(replayAgui).toHaveBeenCalledWith(CONVERSATION, 2)
    // Reconnect resumes with the old opaque cursor after the REST watermark.
    scheduler.runNext()
    await tick()
    expect(transport.requests[1]).toEqual({ conversationId: CONVERSATION, cursor: '7-2' })
    transport.emit({ type: 'event', event: aguiChunk(2), cursor: '7-2' }, 1)
    transport.emit({ type: 'event', event: aguiChunk(5), cursor: '7-5' }, 1)
    transport.emit({ type: 'event', event: aguiChunk(6), cursor: '7-6' }, 1)
    expect(callbacks.onEvent.mock.calls.map(([ev]) => ev)).toEqual([
      aguiChunk(1), aguiChunk(2), aguiChunk(3), aguiChunk(4), aguiChunk(6),
    ])
    await session.dispose()
  })

  it('still reconnects when the agui recovery replay fails', async () => {
    const { session, transport, scheduler, callbacks } = await setupAgui({
      replayAgui: vi.fn(async () => { throw new Error('offline') }),
    })
    transport.emit({ type: 'close', code: 4410, reason: 'stream_too_slow' })
    await tick()
    expect(callbacks.onError).toHaveBeenCalledWith({ code: 'replay_failed' })
    scheduler.runNext()
    await tick()
    expect(transport.requests).toHaveLength(2)
    await session.dispose()
  })

  it('closes 4401/4412 as terminal states in agui mode (no reconnect)', async () => {
    const auth = await setupAgui()
    auth.transport.emit({ type: 'close', code: 4401, reason: 'authentication_epoch_changed' })
    expect(auth.callbacks.onAuthenticationRequired).toHaveBeenCalledTimes(1)
    expect(auth.scheduler.pending).toHaveLength(0)
    await auth.session.dispose()

    const revoked = await setupAgui()
    revoked.transport.emit({ type: 'close', code: 4412, reason: 'binding_revoked' })
    expect(revoked.callbacks.onClosed).toHaveBeenCalledWith({ code: 4412, reason: 'binding_revoked' })
    expect(revoked.scheduler.pending).toHaveLength(0)
    await revoked.session.dispose()
  })

  it('subscribes with the persisted initial cursor (cursor resume)', async () => {
    const { session, transport, callbacks } = await setupAgui({ initialCursor: '7-9' })
    expect(transport.requests[0]).toEqual({ conversationId: CONVERSATION, cursor: '7-9' })
    // Server gap replay frames below the resumed seq are dropped.
    transport.emit({ type: 'event', event: aguiChunk(8), cursor: '7-8' })
    transport.emit({ type: 'event', event: aguiChunk(10), cursor: '7-10' })
    expect(callbacks.onEvent.mock.calls.map(([ev]) => ev)).toEqual([aguiChunk(10)])
    await session.dispose()
  })

  it('treats an invalid initial cursor as a cold start', async () => {
    const { transport } = await setupAgui({ initialCursor: 'nope' })
    expect(transport.requests[0]).toEqual({ conversationId: CONVERSATION })
  })

  it('rejects an agui global stream (no id to deduplicate)', () => {
    expect(() => new AgentStreamSession<AguiEvent>(
      null,
      aguiSpies(),
      { transport: new GenericTransport<AguiEvent>(), scheduler: new FakeScheduler(), replayAgui: vi.fn() },
    )).toThrow()
  })

  it('cross-checks canonical envelope seq against database_seq (duplicate deltas)', async () => {
    const { transport, callbacks } = await setup()
    // Mismatched cursor/database_seq is a duplicate projection: dropped.
    transport.emit({ type: 'event', event: event(3), cursor: '7-4' })
    expect(callbacks.onEvent).not.toHaveBeenCalled()
    transport.emit({ type: 'event', event: event(3), cursor: '7-3' })
    expect(deliveredSeqs(callbacks)).toEqual([3])
  })
})
