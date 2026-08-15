import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  clearNativeCredentials: vi.fn(),
  load: vi.fn(),
  platformValue: 'windows',
}))

vi.mock('@tauri-apps/plugin-clipboard-manager', () => ({ writeText: vi.fn() }))
vi.mock('@tauri-apps/plugin-os', () => ({ arch: () => 'x86_64', platform: () => mocks.platformValue }))
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
    mocks.platformValue = 'windows'
    sessionStorage.clear()
  })

  it('restores native access through dashboard and clears the keyring on logout', async () => {
    const runtime = await createTauriRuntime()

    await expect(runtime.api.sessions.status()).resolves.toMatchObject({ authenticated: true })
    await expect(runtime.api.sessions.logout()).resolves.toEqual({ ok: true })

    expect(mocks.request).toHaveBeenCalledWith('/api/v1/dashboard', expect.objectContaining({ method: 'GET' }))
    expect(mocks.clearNativeCredentials).toHaveBeenCalledWith('https://relay.example')
    expect(mocks.request).not.toHaveBeenCalledWith('/api/v1/admin/session', expect.anything())
  })

  it('injects the voice capability gated by mobile platform and capture support', async () => {
    mocks.platformValue = 'android'
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: () => undefined }, configurable: true })
    try {
      const runtime = await createTauriRuntime()

      expect(runtime.voice).toBeDefined()
      expect(runtime.voice?.enabled()).toBe(true)
      expect(typeof runtime.voice?.uploadAudio).toBe('function')
      expect(runtime.voice?.createRecorder()).toMatchObject({
        start: expect.any(Function),
        stop: expect.any(Function),
        abort: expect.any(Function),
      })
    } finally {
      delete (navigator as { mediaDevices?: unknown }).mediaDevices
    }
  })

  it('keeps the injected voice capability hidden on desktop platforms', async () => {
    mocks.platformValue = 'windows'
    const runtime = await createTauriRuntime()

    expect(runtime.voice).toBeDefined()
    expect(runtime.voice?.enabled()).toBe(false)
  })

  it('keeps the voice gate false without a capture pipeline even on mobile', async () => {
    mocks.platformValue = 'ios'
    // jsdom ships no navigator.mediaDevices — enabled() must stay false.
    const runtime = await createTauriRuntime()

    expect(runtime.voice?.enabled()).toBe(false)
  })

  it('round-trips the pending voice draft through the injected session store', async () => {
    const runtime = await createTauriRuntime()
    const payload = JSON.stringify({ draftId: 'draft-1', text: '待确认的转写' })

    runtime.voice?.draftStore.setItem('termflow.voice.draft', payload)
    expect(runtime.voice?.draftStore.getItem('termflow.voice.draft')).toBe(payload)
    expect(sessionStorage.getItem('termflow.voice.draft')).toBe(payload)
    runtime.voice?.draftStore.removeItem('termflow.voice.draft')
    expect(runtime.voice?.draftStore.getItem('termflow.voice.draft')).toBeNull()
    expect(sessionStorage.getItem('termflow.voice.draft')).toBeNull()
  })
})
