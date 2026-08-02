import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invoke, connect, socket } = vi.hoisted(() => ({
  invoke: vi.fn(),
  connect: vi.fn(),
  socket: {
    addListener: vi.fn(() => vi.fn()),
    send: vi.fn(),
    disconnect: vi.fn(),
  },
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))
vi.mock('@tauri-apps/plugin-websocket', () => ({ default: { connect } }))

import { serverConfig } from '../serverConfig'
import { createTauriTerminalTransport } from './tauriTerminalTransport'

describe('createTauriTerminalTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    connect.mockReset()
    socket.addListener.mockClear()
    serverConfig.current = 'https://b.example'
    invoke.mockResolvedValue({ authorization: 'DPoP access', dpop: 'proof' })
    connect.mockResolvedValue(socket)
  })

  it('signs the HTTPS upgrade target while connecting to the WSS endpoint', async () => {
    await createTauriTerminalTransport().connect({ termId: 'term/one' }, vi.fn())

    expect(invoke).toHaveBeenCalledWith('native_request_headers', {
      issuer: 'https://b.example',
      method: 'GET',
      url: 'https://b.example/api/v1/terms/term%2Fone/terminal',
    })
    expect(connect).toHaveBeenCalledWith(
      'wss://b.example/api/v1/terms/term%2Fone/terminal',
      { headers: { Authorization: 'DPoP access', DPoP: 'proof' } },
    )
  })
})
