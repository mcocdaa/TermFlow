import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invoke } = vi.hoisted(() => ({
  invoke: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke, Channel: class Channel<T> { onmessage: ((message: T) => void) | undefined } }))

import { serverConfig } from '../serverConfig'
import { createTauriTerminalTransport } from './tauriTerminalTransport'

interface NativeCall {
  onMessage?: { onmessage?: (message: unknown) => void }
}

function nativeCall(): NativeCall {
  return (invoke.mock.calls[0]?.[1] ?? {}) as NativeCall
}

describe('createTauriTerminalTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('connects through the native channel with the upgraded WSS endpoint', async () => {
    invoke.mockResolvedValue('socket-1')

    await createTauriTerminalTransport().connect({ termId: 'term/one' }, vi.fn())

    expect(invoke).toHaveBeenCalledWith('native_terminal_connect', expect.objectContaining({
      issuer: 'https://b.example',
      proofUrl: 'https://b.example/api/v1/terms/term%2Fone/terminal',
      socketUrl: 'wss://b.example/api/v1/terms/term%2Fone/terminal',
    }))
    expect(nativeCall().onMessage).toBeDefined()
  })

  it('routes text and binary frames to the transport events', async () => {
    invoke.mockResolvedValue('socket-1')
    const events: unknown[] = []
    const emit = (event: unknown) => events.push(event)
    const connection = await createTauriTerminalTransport().connect({ termId: 'term/one' }, emit)
    const onmessage = nativeCall().onMessage?.onmessage?.bind(null)

    onmessage?.({ type: 'Text', data: 'hello' })
    onmessage?.({ type: 'Binary', data: [1, 2, 3] })
    onmessage?.({ type: 'Close', data: { code: 1006 } })

    expect(events).toEqual([
      { type: 'open' },
      { type: 'text', data: 'hello' },
      { type: 'binary', data: new Uint8Array([1, 2, 3]) },
      { type: 'close', code: 1006 },
    ])

    await connection.sendText('input')
    await connection.sendBinary(new Uint8Array([9]))
    await connection.close(1000, 'done')
    expect(invoke).toHaveBeenCalledWith('native_terminal_send', { id: 'socket-1', data: [105, 110, 112, 117, 116], isBinary: false })
    expect(invoke).toHaveBeenCalledWith('native_terminal_send', { id: 'socket-1', data: [9], isBinary: true })
    expect(invoke).toHaveBeenCalledWith('native_terminal_close', { id: 'socket-1' })
  })
})
