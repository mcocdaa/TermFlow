import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  clearNativeCredentials: vi.fn(),
  load: vi.fn(),
}))

vi.mock('@tauri-apps/plugin-clipboard-manager', () => ({ writeText: vi.fn() }))
vi.mock('@tauri-apps/plugin-os', () => ({ arch: () => 'x86_64', platform: () => 'windows' }))
vi.mock('./adapters/tauriHttpTransport', () => ({ createTauriHttpTransport: () => ({ request: mocks.request }) }))
vi.mock('./adapters/tauriTerminalTransport', () => ({ createTauriTerminalTransport: () => ({}) }))
vi.mock('./adapters/tauriCredentialVault', () => ({ clearNativeCredentials: mocks.clearNativeCredentials }))
vi.mock('./serverConfig', () => ({
  serverConfig: { current: 'https://relay.example', load: mocks.load },
}))

import { createTauriRuntime } from './runtime'

describe('createTauriRuntime', () => {
  beforeEach(() => {
    mocks.request.mockReset().mockResolvedValue({ status: 200, headers: new Headers(), body: { metrics: {}, computers: [] } })
    mocks.clearNativeCredentials.mockReset().mockResolvedValue(undefined)
    mocks.load.mockReset().mockResolvedValue(undefined)
  })

  it('restores native access through dashboard and clears the keyring on logout', async () => {
    const runtime = await createTauriRuntime()

    await expect(runtime.api.sessions.status()).resolves.toMatchObject({ authenticated: true })
    await expect(runtime.api.sessions.logout()).resolves.toEqual({ ok: true })

    expect(mocks.request).toHaveBeenCalledWith('/api/v1/dashboard', expect.objectContaining({ method: 'GET' }))
    expect(mocks.clearNativeCredentials).toHaveBeenCalledWith('https://relay.example')
    expect(mocks.request).not.toHaveBeenCalledWith('/api/v1/admin/session', expect.anything())
  })
})
