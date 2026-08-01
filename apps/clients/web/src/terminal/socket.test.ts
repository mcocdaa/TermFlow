import { describe, expect, it, vi } from 'vitest'
import { TerminalSocket, type TerminalSocketCallbacks } from './socket'

class FakeWebSocket {
  static readonly OPEN = 1
  binaryType = ''
  readyState = FakeWebSocket.OPEN
  sent: Array<string | ArrayBuffer | ArrayBufferView> = []
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  constructor(public readonly url: string) {}
  send(data: string | ArrayBuffer | ArrayBufferView) { this.sent.push(data) }
  close() { this.readyState = 3 }
  open() { this.onopen?.(new Event('open')) }
  message(data: unknown) { this.onmessage?.(new MessageEvent('message', { data })) }
  closed(code = 1006) { this.readyState = 3; this.onclose?.({ code } as CloseEvent) }
}

function setup() {
  const sockets: FakeWebSocket[] = []
  const callbacks: TerminalSocketCallbacks = {
    onStatus: vi.fn(), onReady: vi.fn(), onOutput: vi.fn(), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(), onAuthenticationRequired: vi.fn(),
  }
  const terminal = new TerminalSocket('term /7', callbacks, {
    baseUrl: new URL('https://control.example/app'),
    createWebSocket: (url) => { const socket = new FakeWebSocket(url); sockets.push(socket); return socket as unknown as WebSocket },
    reconnectDelayMs: 25,
  })
  terminal.connect()
  return { terminal, socket: sockets[0], sockets, callbacks }
}

describe('TerminalSocket', () => {
  it('opens the same-origin cookie-authenticated endpoint and handles authoritative controls plus binary output', () => {
    const { socket, callbacks } = setup()
    expect(socket.url).toBe('wss://control.example/api/v1/terms/term%20%2F7/terminal')
    expect(socket.binaryType).toBe('arraybuffer')
    socket.open()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 42, cols: 132, stream_id: 'stream-1' }))
    socket.message(JSON.stringify({ type: 'terminal.size', terminal_id: 'terminal-1', rows: 48, cols: 160 }))
    socket.message(new Uint8Array([27, 91, 72]).buffer)
    expect(callbacks.onReady).toHaveBeenCalledWith(expect.objectContaining({ rows: 42, cols: 132 }))
    expect(callbacks.onSize).toHaveBeenCalledWith({ rows: 48, cols: 160 })
    expect(callbacks.onOutput).toHaveBeenCalledWith(new Uint8Array([27, 91, 72]))
  })

  it('chunks UTF-8 input and paste into binary frames no larger than 65536 bytes', () => {
    const { terminal, socket } = setup()
    socket.open()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 24, cols: 80, stream_id: 'stream-1' }))
    terminal.sendInput('界'.repeat(50_000))
    const frames = socket.sent.filter((frame): frame is Uint8Array => ArrayBuffer.isView(frame))
    expect(frames.length).toBeGreaterThan(1)
    expect(Math.max(...frames.map((frame) => frame.byteLength))).toBeLessThanOrEqual(65_536)
    expect(frames.reduce((total, frame) => total + frame.byteLength, 0)).toBe(new TextEncoder().encode('界'.repeat(50_000)).byteLength)
  })

  it('resets before output when B reports a gap on a replacement stream', () => {
    const { socket, callbacks } = setup()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 24, cols: 80, stream_id: 'stream-1' }))
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-2', rows: 24, cols: 80, stream_id: 'stream-2', gap: true }))
    expect(callbacks.onReset).toHaveBeenCalledTimes(1)
  })

  it('uses the public semantic action envelope and binding list', () => {
    const { terminal, socket, callbacks } = setup()
    socket.open()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 24, cols: 80, stream_id: 'stream-1' }))
    socket.message(JSON.stringify({ type: 'terminal.binding_snapshot', terminal_id: 'terminal-1', prefix: 'C-a', prefix2: null, bindings: [{ action: 'copy_mode', key: 'C-a [', tooltip: '进入复制模式' }] }))
    expect(callbacks.onBindings).toHaveBeenCalledWith(expect.objectContaining({ prefix: 'C-a', bindings: [expect.objectContaining({ action: 'copy_mode' })] }))
    terminal.sendAction('copy_mode', { targetPaneId: '%1' })
    const frame = JSON.parse(socket.sent.find((item): item is string => typeof item === 'string')!)
    expect(frame).toMatchObject({ type: 'terminal.action', action: 'copy_mode', target_pane_id: '%1', confirmed: false })
    expect(frame.action_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(frame).not.toHaveProperty('request_id')
  })

  it('drops input before terminal.ready and immediately after terminal.closed', () => {
    const { terminal, socket } = setup()
    socket.open()
    terminal.sendInput('before-ready')
    terminal.sendAction('new_window')
    expect(socket.sent).toHaveLength(0)
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 24, cols: 80, stream_id: 'stream-1' }))
    terminal.sendInput('ready')
    expect(socket.sent).toHaveLength(1)
    socket.message(JSON.stringify({ type: 'terminal.closed', terminal_id: 'terminal-1', reason: 'stream_gap' }))
    terminal.sendInput('after-close')
    expect(socket.sent).toHaveLength(1)
  })

  it('reconnects unexpected closes but stops after replacement or explicit disposal', async () => {
    vi.useFakeTimers()
    const { terminal, socket, sockets, callbacks } = setup()
    socket.closed()
    expect(callbacks.onStatus).toHaveBeenCalledWith('reconnecting')
    await vi.advanceTimersByTimeAsync(25)
    expect(sockets).toHaveLength(2)
    sockets[1].closed()
    await vi.advanceTimersByTimeAsync(49)
    expect(sockets).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(sockets).toHaveLength(3)
    sockets[2].message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-2', rows: 24, cols: 80, stream_id: 'stream-2' }))
    sockets[2].message(JSON.stringify({ type: 'terminal.closed', terminal_id: 'terminal-2', reason: 'replaced' }))
    sockets[2].closed(1000)
    await vi.advanceTimersByTimeAsync(100)
    expect(sockets).toHaveLength(3)
    terminal.dispose()
    expect(sockets[2].readyState).toBe(3)
    vi.useRealTimers()
  })

  it('resumes the exact terminal stream and output sequence after an unexpected close', async () => {
    vi.useFakeTimers()
    const { socket, sockets } = setup()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', rows: 24, cols: 80, stream_id: '22222222-2222-4222-8222-222222222222' }))
    socket.message(new Uint8Array([1]).buffer)
    socket.message(new Uint8Array([2]).buffer)

    socket.closed()
    await vi.advanceTimersByTimeAsync(25)

    expect(sockets).toHaveLength(2)
    const resumed = new URL(sockets[1].url)
    expect(resumed.searchParams.get('terminal_id')).toBe('11111111-1111-4111-8111-111111111111')
    expect(resumed.searchParams.get('stream_id')).toBe('22222222-2222-4222-8222-222222222222')
    expect(resumed.searchParams.get('after_seq')).toBe('2')
    vi.useRealTimers()
  })

  it('requests an explicit terminal detach when the route is disposed', () => {
    const { terminal, socket } = setup()
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', rows: 24, cols: 80, stream_id: '22222222-2222-4222-8222-222222222222' }))

    terminal.dispose()

    expect(JSON.parse(socket.sent[0] as string)).toEqual({ type: 'terminal.close', reason: 'client_closed' })
  })

  it('stops reconnecting for rejected browser sessions and distinguishes authentication from Origin rejection', async () => {
    vi.useFakeTimers()
    const expired = setup()
    expired.socket.closed(4401)
    expect(expired.callbacks.onAuthenticationRequired).toHaveBeenCalledTimes(1)
    expect(expired.callbacks.onStatus).toHaveBeenCalledWith('closed')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(expired.sockets).toHaveLength(1)

    const forbidden = setup()
    forbidden.socket.closed(4403)
    expect(forbidden.callbacks.onAuthenticationRequired).not.toHaveBeenCalled()
    expect(forbidden.callbacks.onError).toHaveBeenCalledWith({ code: 'origin_rejected' })
    await vi.advanceTimersByTimeAsync(20_000)
    expect(forbidden.sockets).toHaveLength(1)
    vi.useRealTimers()
  })

  it('ignores malformed and unknown text controls without echoing or logging them', () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { socket, callbacks } = setup()
    socket.message('{token-output')
    socket.message(JSON.stringify({ type: 'terminal.unknown', secret: 'do-not-log' }))
    socket.message(JSON.stringify({ type: 'terminal.size', rows: 1, cols: 1 }))
    socket.message(JSON.stringify({ type: 'terminal.error', code: 'bad', message: 'missing terminal' }))
    socket.message(JSON.stringify({ type: 'terminal.closed', reason: 'replaced' }))
    expect(callbacks.onOutput).not.toHaveBeenCalled()
    expect(callbacks.onSize).not.toHaveBeenCalled()
    expect(callbacks.onError).not.toHaveBeenCalled()
    expect(callbacks.onClosed).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })

  it('ignores controls for a stale or not-yet-ready terminal identity', () => {
    const { socket, callbacks } = setup()
    socket.message(JSON.stringify({ type: 'terminal.size', terminal_id: 'terminal-1', rows: 1, cols: 1 }))
    socket.message(JSON.stringify({ type: 'terminal.ready', terminal_id: 'terminal-1', rows: 24, cols: 80, stream_id: 'stream-1' }))
    socket.message(JSON.stringify({ type: 'terminal.size', terminal_id: 'terminal-old', rows: 2, cols: 2 }))
    socket.message(JSON.stringify({ type: 'terminal.error', terminal_id: 'terminal-old', code: 'bad', message: 'stale' }))
    socket.message(JSON.stringify({ type: 'terminal.closed', terminal_id: 'terminal-old', reason: 'replaced' }))
    expect(callbacks.onSize).not.toHaveBeenCalled()
    expect(callbacks.onError).not.toHaveBeenCalled()
    expect(callbacks.onClosed).not.toHaveBeenCalled()
  })
})
