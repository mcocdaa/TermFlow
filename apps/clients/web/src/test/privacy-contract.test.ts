import { describe, expect, it, vi } from 'vitest'
import { TerminalSession, type TerminalSessionCallbacks } from '@termflow/client-core'
import { createBrowserTerminalTransport } from '../adapters/browserTerminalTransport'

describe('privacy contracts', () => {
  it('keeps terminal output out of storage, URL, console, and telemetry-shaped globals', () => {
    const outputSample = 'PRIVATE_TERMINAL_OUTPUT_728'
    const received: string[] = []
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const callbacks: TerminalSessionCallbacks = {
      onStatus: vi.fn(), onReady: vi.fn(), onOutput: (bytes) => received.push(new TextDecoder().decode(bytes)), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(), onAuthenticationRequired: vi.fn(),
    }
    const fakeSocket = { binaryType: '', readyState: 1, send: vi.fn(), close: vi.fn(), onmessage: null as ((event: MessageEvent) => void) | null, onopen: null, onclose: null, onerror: null }
    const session = new TerminalSession('term-privacy', callbacks, {
      transport: createBrowserTerminalTransport({ createWebSocket: () => fakeSocket as unknown as WebSocket }),
      scheduler: { set: () => 1, clear: () => undefined },
      createId: () => '33333333-3333-4333-8333-333333333333',
    })
    session.connect()
    fakeSocket.onmessage?.({ data: JSON.stringify({
      type: 'terminal.ready',
      terminal_id: '11111111-1111-4111-8111-111111111111',
      stream_id: '22222222-2222-4222-8222-222222222222',
      rows: 24,
      cols: 80,
    }) } as MessageEvent)
    fakeSocket.onmessage?.({ data: new TextEncoder().encode(outputSample) } as MessageEvent)
    expect(received).toEqual([outputSample])
    expect(JSON.stringify([...Array(localStorage.length)].map((_, index) => localStorage.getItem(localStorage.key(index)!)))).not.toContain(outputSample)
    expect(JSON.stringify([...Array(sessionStorage.length)].map((_, index) => sessionStorage.getItem(sessionStorage.key(index)!)))).not.toContain(outputSample)
    expect(window.location.href).not.toContain(outputSample)
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect((globalThis as Record<string, unknown>).telemetry).toBeUndefined()
    session.dispose()
  })
})
