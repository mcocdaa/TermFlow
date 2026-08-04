import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  onOpenUrl: vi.fn(),
  openUrl: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))
vi.mock('@tauri-apps/plugin-deep-link', () => ({ onOpenUrl: mocks.onOpenUrl }))
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: mocks.openUrl }))

import { pollDeviceAuthorization, tauriAuthorizationBrowser } from './tauriAuthorization'

describe('tauriAuthorizationBrowser', () => {
  it('registers the deep-link listener before opening the system browser', async () => {
    let receive: ((urls: string[]) => void) | undefined
    let registrationReady: ((unlisten: () => void) => void) | undefined
    mocks.onOpenUrl.mockImplementation((callback: (urls: string[]) => void) => {
      receive = callback
      return new Promise(resolve => { registrationReady = resolve })
    })
    mocks.openUrl.mockResolvedValue(undefined)

    const callback = tauriAuthorizationBrowser.waitForCallback('state-1')
    const opened = tauriAuthorizationBrowser.open('https://b.example/api/v1/oauth/authorize')
    await Promise.resolve()
    expect(mocks.openUrl).not.toHaveBeenCalled()

    registrationReady?.(vi.fn())
    await opened
    expect(mocks.openUrl).toHaveBeenCalledOnce()

    receive?.(['termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111'])
    await expect(callback).resolves.toContain('transaction_id=')
  })

  it('records the sanitized system-browser error detail', async () => {
    let registrationReady: ((unlisten: () => void) => void) | undefined
    mocks.onOpenUrl.mockImplementation(() => new Promise(resolve => { registrationReady = resolve }))
    mocks.openUrl.mockRejectedValueOnce(new Error('ShellExecute failed for https://relay.example/auth?code=secret'))

    const callback = tauriAuthorizationBrowser.waitForCallback('state-2')
    const opened = tauriAuthorizationBrowser.open('https://relay.example/api/v1/oauth/authorize?state=state-2')
    await Promise.resolve()
    registrationReady?.(vi.fn())
    await expect(opened).rejects.toThrow('ShellExecute failed')
    await Promise.resolve()

    expect(mocks.invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
      event: 'browser_open_failed',
      errorCode: 'browser_open_failed',
      errorDetail: 'Error: ShellExecute failed for <url>',
    }))
    callback.then(() => undefined, () => undefined)
  })
})

describe('pollDeviceAuthorization', () => {
  it('uses the native device exchange command without opening a browser', async () => {
    mocks.openUrl.mockClear()
    mocks.invoke.mockResolvedValue({ access_token: 'a', token_type: 'DPoP', expires_in: 60, scopes: [] })
    await expect(pollDeviceAuthorization({ issuer: 'https://relay.example.com', deviceCode: 'device', codeVerifier: 'verifier', publicJwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' } })).resolves.toMatchObject({ access_token: 'a' })
    expect(mocks.invoke).toHaveBeenCalledWith('native_exchange_device_code', { issuer: 'https://relay.example.com', deviceCode: 'device', codeVerifier: 'verifier', publicJwk: expect.any(Object) })
    expect(mocks.openUrl).not.toHaveBeenCalled()
  })
})
