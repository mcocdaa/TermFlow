import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  onOpenUrl: vi.fn(),
  openUrl: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))
vi.mock('@tauri-apps/plugin-deep-link', () => ({ onOpenUrl: mocks.onOpenUrl }))
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: mocks.openUrl }))

import { exchangeAuthorization, pollDeviceAuthorization, tauriAuthorizationBrowser } from './tauriAuthorization'

const deepLinkBrowser = () => tauriAuthorizationBrowser({ issuer: 'https://b.example', loopback: false })
const loopbackBrowser = () => tauriAuthorizationBrowser({ issuer: 'https://b.example', loopback: true })

beforeEach(() => {
  mocks.invoke.mockReset().mockResolvedValue(undefined)
  mocks.openUrl.mockReset().mockResolvedValue(undefined)
  mocks.onOpenUrl.mockReset().mockImplementation(() => Promise.resolve(vi.fn()))
})

describe('tauriAuthorizationBrowser', () => {
  it('registers the deep-link listener before opening the system browser', async () => {
    let receive: ((urls: string[]) => void) | undefined
    let registrationReady: ((unlisten: () => void) => void) | undefined
    mocks.onOpenUrl.mockImplementation((callback: (urls: string[]) => void) => {
      receive = callback
      return new Promise(resolve => { registrationReady = resolve })
    })
    mocks.openUrl.mockResolvedValue(undefined)

    const browser = deepLinkBrowser()
    const callback = browser.waitForCallback('state-1')
    const opened = browser.open('https://b.example/api/v1/oauth/authorize')
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

    const browser = deepLinkBrowser()
    const callback = browser.waitForCallback('state-2')
    const opened = browser.open('https://relay.example/api/v1/oauth/authorize?state=state-2')
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
    const browser = deepLinkBrowser()
    const callback = browser.waitForCallback('state-3')
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

    await expect(deepLinkBrowser().waitForCallback('state-aborted', controller.signal))
      .rejects.toThrow('authorization_cancelled')
    expect(mocks.onOpenUrl).not.toHaveBeenCalled()
  })

  it('rejects with an actionable timeout when no callback arrives', async () => {
    vi.useFakeTimers()
    try {
      mocks.onOpenUrl.mockClear()
      mocks.invoke.mockClear()
      mocks.onOpenUrl.mockImplementation(() => new Promise(() => undefined))

      const callback = deepLinkBrowser().waitForCallback('state-timeout')
      const settled = vi.fn()
      void callback.then(() => settled('resolved'), (error: Error) => settled(error.message))
      await Promise.resolve()
      expect(settled).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(5 * 60 * 1000)

      expect(settled).toHaveBeenCalledWith('authorization_callback_timeout')
      expect(mocks.invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
        event: 'authorization_callback_timeout',
        errorCode: 'authorization_callback_timeout',
      }))
    } finally {
      vi.useRealTimers()
    }
  })

  it('binds the loopback listener and returns the redirect URI for desktop', async () => {
    mocks.invoke.mockResolvedValueOnce(51234)
    await expect(loopbackBrowser().prepareCallback?.('state-1')).resolves.toBe('http://127.0.0.1:51234/oauth/callback')
    expect(mocks.invoke).toHaveBeenCalledWith('native_bind_authorization_listener', { expectedState: 'state-1', issuer: 'https://b.example' })
  })

  it('keeps the app-scheme redirect when loopback is disabled', async () => {
    mocks.invoke.mockClear()
    await expect(deepLinkBrowser().prepareCallback?.('state-1')).resolves.toBeUndefined()
    expect(mocks.invoke).not.toHaveBeenCalled()
  })

  it('falls back to the deep link when the loopback bind fails', async () => {
    mocks.invoke.mockReset()
    mocks.invoke.mockRejectedValueOnce(new Error('listener_bind_failed'))
    mocks.onOpenUrl.mockImplementation(() => Promise.resolve(vi.fn()))
    mocks.openUrl.mockResolvedValue(undefined)

    const browser = loopbackBrowser()
    await expect(browser.prepareCallback?.('state-fallback')).resolves.toBeUndefined()
    expect(mocks.invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({ event: 'loopback_listener_bind_failed' }))

    const callback = browser.waitForCallback('state-fallback')
    await browser.open('https://b.example/api/v1/oauth/authorize?state=state-fallback')
    await Promise.resolve()
    expect(mocks.openUrl).toHaveBeenCalledOnce()
    mocks.openUrl.mockClear()
    receiveDeepLink('state-fallback')
    await expect(callback).resolves.toContain('transaction_id=')
  })

  it('waits on the loopback callback and cancels the listener on abort', async () => {
    mocks.invoke.mockResolvedValueOnce(51234)
    const browser = loopbackBrowser()
    await browser.prepareCallback?.('state-loopback')

    mocks.invoke.mockResolvedValueOnce('http://127.0.0.1:51234/oauth/callback?state=state-loopback&transaction_id=11111111-1111-4111-8111-111111111111')
    await expect(browser.waitForCallback('state-loopback')).resolves.toContain('transaction_id=')
    expect(mocks.invoke).toHaveBeenCalledWith('native_wait_authorization_callback', { expectedState: 'state-loopback' })

    mocks.invoke.mockResolvedValueOnce(51235)
    await browser.prepareCallback?.('state-cancel')
    mocks.invoke.mockImplementationOnce(() => new Promise<never>(() => undefined))
    const controller = new AbortController()
    const cancelled = browser.waitForCallback('state-cancel', controller.signal)
    await Promise.resolve()
    controller.abort()
    await expect(cancelled).rejects.toThrow('authorization_cancelled')
    expect(mocks.invoke).toHaveBeenCalledWith('native_cancel_authorization_listener', { expectedState: 'state-cancel' })
  })

  it('does not require the deep-link listener when the loopback callback is prepared', async () => {
    mocks.invoke.mockReset()
    mocks.invoke.mockResolvedValueOnce(51236)
    mocks.openUrl.mockResolvedValue(undefined)

    const browser = loopbackBrowser()
    await browser.prepareCallback?.('state-nolisten')
    await expect(browser.open('https://b.example/api/v1/oauth/authorize?state=state-nolisten')).resolves.toBeUndefined()
    expect(mocks.openUrl).toHaveBeenCalledOnce()
    expect(mocks.onOpenUrl).not.toHaveBeenCalled()
  })
})

function receiveDeepLink(state: string) {
  const handler = mocks.onOpenUrl.mock.calls.at(-1)?.[0] as (urls: string[]) => void
  handler(['termflow://auth/callback?state=' + state + '&transaction_id=11111111-1111-4111-8111-111111111111'])
}

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
