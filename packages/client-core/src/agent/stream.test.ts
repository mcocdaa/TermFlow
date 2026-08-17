import { describe, expect, it, vi, type Mock } from 'vitest'
import type { AgentEventResponse } from '@termflow/client-contracts'
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

  it('deduplicates by database_seq within a conversation stream', async () => {
    const { transport, callbacks } = await setup()
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: event(2), cursor: '7-2' })
    transport.emit({ type: 'event', event: event(1), cursor: '7-1' })
    transport.emit({ type: 'event', event: event(3), cursor: '7-3' })
    expect(callbacks.onEvent).toHaveBeenCalledTimes(2)
    expect(deliveredSeqs(callbacks)).toEqual([2, 3])
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

  it('closes without reconnecting when the authentication epoch changes', async () => {
    const { session, transport, scheduler, callbacks } = await setup()
    transport.emit({ type: 'close', code: 4401, reason: 'authentication_epoch_changed' })
    expect(callbacks.onStatus).toHaveBeenLastCalledWith('closed')
    expect(callbacks.onAuthenticationRequired).toHaveBeenCalledTimes(1)
    expect(scheduler.pending).toHaveLength(0)
    await session.dispose()
  })
})
