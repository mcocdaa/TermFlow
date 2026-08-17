import { describe, expect, it, vi } from 'vitest'
import { TerminalSession, type TerminalSessionCallbacks } from './session'
import type {
  TerminalConnectRequest,
  TerminalConnection,
  TerminalTransport,
  TerminalTransportEvent,
} from './ports'

const TERMINAL_1 = '11111111-1111-4111-8111-111111111111'
const STREAM_1 = '22222222-2222-4222-8222-222222222222'
const TERMINAL_2 = '33333333-3333-4333-8333-333333333333'
const STREAM_2 = '44444444-4444-4444-8444-444444444444'
const ACTION_ID = '55555555-5555-4555-8555-555555555555'

class FakeConnection implements TerminalConnection {
  readonly text: string[] = []
  readonly binary: Uint8Array[] = []
  readonly closes: Array<{ code: number, reason: string }> = []
  async sendText(data: string) { this.text.push(data) }
  async sendBinary(data: Uint8Array) { this.binary.push(data) }
  async close(code: number, reason: string) { this.closes.push({ code, reason }) }
}

class FakeTransport implements TerminalTransport {
  readonly requests: TerminalConnectRequest[] = []
  readonly connections: FakeConnection[] = []
  readonly emitters: Array<(event: TerminalTransportEvent) => void> = []
  async connect(request: TerminalConnectRequest, emit: (event: TerminalTransportEvent) => void): Promise<TerminalConnection> {
    const connection = new FakeConnection()
    this.requests.push(request)
    this.connections.push(connection)
    this.emitters.push(emit)
    return connection
  }
  emit(event: TerminalTransportEvent, index = this.emitters.length - 1) { this.emitters[index]?.(event) }
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

function callbackSpies(): TerminalSessionCallbacks {
  return {
    onStatus: vi.fn(), onReady: vi.fn(), onOutput: vi.fn(), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(), onAuthenticationRequired: vi.fn(),
  }
}

async function setup() {
  const transport = new FakeTransport()
  const scheduler = new FakeScheduler()
  const callbacks = callbackSpies()
  const session = new TerminalSession('term /7', callbacks, {
    transport,
    scheduler,
    createId: () => ACTION_ID,
    reconnectDelayMs: 25,
  })
  await session.connect()
  return { session, transport, scheduler, callbacks }
}

function ready(transport: FakeTransport, terminalId = TERMINAL_1, streamId = STREAM_1) {
  transport.emit({ type: 'text', data: JSON.stringify({ type: 'terminal.ready', terminal_id: terminalId, stream_id: streamId, rows: 24, cols: 80 }) })
}

describe('TerminalSession', () => {
  it('waits for terminal.ready, then handles authoritative controls and binary output', async () => {
    const { transport, callbacks } = await setup()
    transport.emit({ type: 'open' })
    ready(transport)
    transport.emit({ type: 'text', data: JSON.stringify({ type: 'terminal.size', terminal_id: TERMINAL_1, rows: 48, cols: 160 }) })
    transport.emit({ type: 'binary', data: Uint8Array.of(27, 91, 72) })

    expect(callbacks.onReady).toHaveBeenCalledWith(expect.objectContaining({ rows: 24, cols: 80 }))
    expect(callbacks.onSize).toHaveBeenCalledWith({ rows: 48, cols: 160 })
    expect(callbacks.onOutput).toHaveBeenCalledWith(Uint8Array.of(27, 91, 72))
  })

  it('chunks UTF-8 input into binary frames no larger than 65536 bytes', async () => {
    const { session, transport } = await setup()
    ready(transport)
    await session.sendInput('界'.repeat(50_000))
    const frames = transport.connections[0]?.binary ?? []
    expect(frames.length).toBeGreaterThan(1)
    expect(Math.max(...frames.map((frame) => frame.byteLength))).toBeLessThanOrEqual(65_536)
    expect(frames.reduce((total, frame) => total + frame.byteLength, 0)).toBe(new TextEncoder().encode('界'.repeat(50_000)).byteLength)
  })

  it('reconnects with exponential delay capped at ten seconds and stops after replacement', async () => {
    const { session, transport, scheduler } = await setup()
    transport.emit({ type: 'close', code: 1006 })
    expect(scheduler.pending[0]?.delay).toBe(25)
    scheduler.runNext()
    await Promise.resolve()
    transport.emit({ type: 'close', code: 1006 })
    expect(scheduler.pending[1]?.delay).toBe(50)
    scheduler.runNext()
    await Promise.resolve()
    ready(transport, TERMINAL_2, STREAM_2)
    transport.emit({ type: 'text', data: JSON.stringify({ type: 'terminal.closed', terminal_id: TERMINAL_2, reason: 'replaced' }) })
    transport.emit({ type: 'close', code: 1000 })
    expect(scheduler.pending).toHaveLength(2)

    const cappedTransport = new FakeTransport()
    const cappedScheduler = new FakeScheduler()
    const capped = new TerminalSession('term', callbackSpies(), { transport: cappedTransport, scheduler: cappedScheduler, createId: () => ACTION_ID, reconnectDelayMs: 8_000 })
    await capped.connect()
    cappedTransport.emit({ type: 'close', code: 1006 })
    expect(cappedScheduler.pending[0]?.delay).toBe(8_000)
    cappedScheduler.runNext()
    await Promise.resolve()
    cappedTransport.emit({ type: 'close', code: 1006 })
    expect(cappedScheduler.pending[1]?.delay).toBe(10_000)
    await capped.dispose()
    await session.dispose()
  })
})
