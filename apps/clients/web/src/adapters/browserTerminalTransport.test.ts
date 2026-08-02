import { describe, expect, it, vi } from 'vitest'
import type { TerminalTransportEvent } from '@termflow/client-core'
import { createBrowserTerminalTransport } from './browserTerminalTransport'

class FakeWebSocket {
  binaryType = ''
  readonly sent: unknown[] = []
  readonly closes: Array<{ code?: number, reason?: string }> = []
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  constructor(readonly url: string) {}
  send(data: unknown) { this.sent.push(data) }
  close(code?: number, reason?: string) { this.closes.push({ code, reason }) }
}

describe('createBrowserTerminalTransport', () => {
  it('builds the same-origin WebSocket URL with the exact resume tuple', async () => {
    let socket!: FakeWebSocket
    const events: TerminalTransportEvent[] = []
    const transport = createBrowserTerminalTransport({
      baseUrl: new URL('https://control.example/app'),
      createWebSocket: (url) => { socket = new FakeWebSocket(url); return socket as unknown as WebSocket },
    })
    const connection = await transport.connect({
      termId: 'term /7',
      terminalId: '11111111-1111-4111-8111-111111111111',
      streamId: '22222222-2222-4222-8222-222222222222',
      afterSeq: 9,
    }, (event) => events.push(event))

    expect(socket.url).toBe('wss://control.example/api/v1/terms/term%20%2F7/terminal?terminal_id=11111111-1111-4111-8111-111111111111&stream_id=22222222-2222-4222-8222-222222222222&after_seq=9')
    expect(socket.binaryType).toBe('arraybuffer')
    await connection.sendText('text')
    await connection.sendBinary(Uint8Array.of(1, 2))
    await connection.close(1000, 'done')
    expect(socket.sent).toEqual(['text', Uint8Array.of(1, 2)])
    expect(socket.closes).toEqual([{ code: 1000, reason: 'done' }])
  })

  it('maps browser events into platform-neutral transport events', async () => {
    let socket!: FakeWebSocket
    const emit = vi.fn()
    createBrowserTerminalTransport({
      baseUrl: new URL('http://127.0.0.1:8000/'),
      createWebSocket: (url) => { socket = new FakeWebSocket(url); return socket as unknown as WebSocket },
    }).connect({ termId: 'term-1' }, emit)
    await Promise.resolve()

    socket.onopen?.(new Event('open'))
    socket.onmessage?.(new MessageEvent('message', { data: 'control' }))
    socket.onmessage?.(new MessageEvent('message', { data: Uint8Array.of(3, 4).buffer }))
    socket.onclose?.({ code: 4401 } as CloseEvent)

    expect(socket.url).toBe('ws://127.0.0.1:8000/api/v1/terms/term-1/terminal')
    expect(emit).toHaveBeenNthCalledWith(1, { type: 'open' })
    expect(emit).toHaveBeenNthCalledWith(2, { type: 'text', data: 'control' })
    expect(emit).toHaveBeenNthCalledWith(3, { type: 'binary', data: Uint8Array.of(3, 4) })
    expect(emit).toHaveBeenNthCalledWith(4, { type: 'close', code: 4401 })
  })
})
