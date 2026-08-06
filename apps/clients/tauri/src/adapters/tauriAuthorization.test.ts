import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  onOpenUrl: vi.fn(),
  openUrl: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))
vi.mock('@tauri-apps/plugin-deep-link', () => ({ onOpenUrl: mocks.onOpenUrl }))
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: mocks.openUrl }))

import { exchangeAuthorization, pollDeviceAuthorization, tauriAuthorizationBrowser } from './tauriAuthorization'

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
    expect(JSON.stringify(mocks.invoke.mock.calls)).not.toContain('secret')
    callback.then(() => undefined, () => undefined)
  })

  it('ignores malformed deep links even when their state matches', async () => {
    let receive: ((urls: string[]) => void) | undefined
    mocks.onOpenUrl.mockImplementation((callback: (urls: string[]) => void) => {
      receive = callback
      return Promise.resolve(vi.fn())
    })
    const callback = tauriAuthorizationBrowser.waitForCallback('state-3')
    let settled = false
    void callback.then(() => { settled = true })
    await Promise.resolve()

    const malformedCallbacks = [
      'https://attacker.example/callback?state=state-3&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth:444/callback?state=state-3&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://user@auth/callback?state=state-3&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth/callback?state=state-3&transaction_id=11111111-1111-4111-8111-111111111111#fragment',
      'termflow://auth/callback?state=state-3&transaction_id=not-a-uuid',
    ]
    for (const malformed of malformedCallbacks) receive?.([malformed])
    await Promise.resolve()
    expect(settled).toBe(false)
    expect(mocks.invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
      event: 'authorization_callback_invalid', errorCode: 'authorization_callback_invalid',
    }))

    receive?.(['termflow://auth/callback?state=state-3&transaction_id=11111111-1111-4111-8111-111111111111'])
    await expect(callback).resolves.toContain('transaction_id=')
  })

  it('rejects a pre-aborted listener without registering a deep link', async () => {
    mocks.onOpenUrl.mockClear()
    const controller = new AbortController()
    controller.abort()

    await expect(tauriAuthorizationBrowser.waitForCallback('state-aborted', controller.signal))
      .rejects.toThrow('authorization_cancelled')
    expect(mocks.onOpenUrl).not.toHaveBeenCalled()
  })
})

describe('pollDeviceAuthorization', () => {
  it('uses the native device exchange command without opening a browser', async () => {
    mocks.openUrl.mockClear()
    mocks.invoke.mockResolvedValue({ accessToken: 'a', expiresAt: '2026-08-05T12:00:00Z', tokenType: 'DPoP' })
    await expect(pollDeviceAuthorization({ issuer: 'https://relay.example.com', deviceCode: 'device', codeVerifier: 'verifier', publicJwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' } })).resolves.toMatchObject({ accessToken: 'a' })
    expect(mocks.invoke).toHaveBeenCalledWith('native_exchange_device_code', { request: { issuer: 'https://relay.example.com', deviceCode: 'device', codeVerifier: 'verifier', publicJwk: expect.any(Object) } })
    expect(mocks.openUrl).not.toHaveBeenCalled()
  })
})

describe('exchangeAuthorization', () => {
  it('records sanitized token exchange details without credentials', async () => {
    mocks.invoke.mockRejectedValueOnce(new Error('token=secret https://relay.example/auth?code=secret'))

    await expect(exchangeAuthorization({
      issuer: 'https://relay.example.com',
      transaction: 'transaction-1',
      verifier: 'verifier-secret',
      redirectUri: 'termflow://auth/callback?code=secret',
    })).rejects.toThrow('token=secret')

    expect(mocks.invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
      event: 'token_exchange_failed',
      errorCode: 'token_exchange_failed',
      errorDetail: 'Error: token=<redacted> <url>',
    }))
    const logCall = mocks.invoke.mock.calls.find(([command]) => command === 'native_log')
    expect(JSON.stringify(logCall)).not.toContain('verifier-secret')
  })
})
