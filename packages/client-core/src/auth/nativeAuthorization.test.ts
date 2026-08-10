import { describe, expect, it, vi } from 'vitest'
import { isValidNativeAuthorizationCallback, NativeAuthorizationSession, parseLoopbackNativeAuthorizationCallback, parseNativeAuthorizationCallback } from './nativeAuthorization'
import type { AuthorizationBrowserPort, CredentialVaultPort, NativeAccessCredential } from './ports'
import type { AuthorizationState } from './authorizationState'

const browser = (callback: string): AuthorizationBrowserPort => ({
  open: vi.fn().mockResolvedValue(undefined),
  waitForCallback: vi.fn().mockResolvedValue(callback),
})

const vault = (): CredentialVaultPort => ({
  load: vi.fn().mockResolvedValue(null),
  replace: vi.fn().mockResolvedValue(undefined),
  clear: vi.fn().mockResolvedValue(undefined),
})

describe('NativeAuthorizationSession', () => {
  it('opens the system browser and accepts only the matching state and transaction', async () => {
    const callback = 'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111'
    const port = browser(callback)
    const store = vault()
    const exchange = vi.fn().mockResolvedValue({
      accessToken: 'access', expiresAt: '2026-08-02T12:00:00Z', tokenType: 'DPoP',
    } satisfies NativeAccessCredential)
    const states: AuthorizationState[] = []
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example',
      authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'TermFlow Desktop', platform: 'linux', version: '0.1.0' },
      scopes: ['terminal.read'],
      browser: port,
      vault: store,
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'state-1',
      exchange,
      onState: (state) => states.push(state),
    })

    await expect(session.authorize()).resolves.toMatchObject({ accessToken: 'access' })
    expect(port.open).toHaveBeenCalledOnce()
    expect(vi.mocked(port.waitForCallback).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(port.open).mock.invocationCallOrder[0]!)
    expect(exchange).toHaveBeenCalledWith(expect.objectContaining({ transaction: '11111111-1111-4111-8111-111111111111', verifier: 'v'.repeat(43) }))
    expect(store.replace).toHaveBeenCalledWith('https://b.example', expect.objectContaining({ accessToken: 'access' }))
    expect(states).toEqual(['requesting', 'pending', 'approved', 'connected'])
  })

  it('uses the prepared loopback callback and accepts the loopback handoff', async () => {
    const callback = 'http://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111'
    const port: AuthorizationBrowserPort = {
      prepareCallback: vi.fn().mockResolvedValue('http://127.0.0.1:51234/oauth/callback'),
      open: vi.fn(),
      waitForCallback: vi.fn().mockResolvedValue(callback),
    }
    const store = vault()
    const exchange = vi.fn().mockResolvedValue({
      accessToken: 'access', expiresAt: '2026-08-02T12:00:00Z', tokenType: 'DPoP',
    } satisfies NativeAccessCredential)
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example',
      authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'TermFlow Desktop', platform: 'windows', version: '0.1.0' },
      scopes: ['terminal.read'],
      browser: port,
      vault: store,
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'state-1',
      exchange,
    })

    await expect(session.authorize()).resolves.toMatchObject({ accessToken: 'access' })
    expect(vi.mocked(port.prepareCallback)).toHaveBeenCalledWith('state-1')
    const opened = vi.mocked(port.open).mock.calls[0]?.[0] ?? ''
    expect(new URL(opened).searchParams.get('redirect_uri')).toBe('http://127.0.0.1:51234/oauth/callback')
    expect(exchange).toHaveBeenCalledWith(expect.objectContaining({ redirectUri: 'http://127.0.0.1:51234/oauth/callback' }))
    expect(store.replace).toHaveBeenCalledWith('https://b.example', expect.objectContaining({ accessToken: 'access' }))
  })

  it('rejects callbacks with the wrong state before token exchange', async () => {
    const exchange = vi.fn()
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: browser('termflow://auth/callback?state=attacker&transaction_id=11111111-1111-4111-8111-111111111111'), vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange,
    })

    await expect(session.authorize()).rejects.toThrow('authorization_callback_invalid')
    expect(exchange).not.toHaveBeenCalled()
  })

  it('rejects callbacks that carry anything beyond state and transaction_id', async () => {
    const exchange = vi.fn()
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: browser('termflow://auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111&code=must-not-leak'), vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange,
    })

    await expect(session.authorize()).rejects.toThrow('authorization_callback_invalid')
    expect(exchange).not.toHaveBeenCalled()
  })

  it('rejects callback authority and fragment variants before token exchange', async () => {
    const exchange = vi.fn()
    const callbacks = [
      'termflow://auth:444/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://user@auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111#fragment',
      'termflow://auth/callback?state=expected&transaction_id=not-a-uuid',
    ]
    for (const callback of callbacks) {
      const session = new NativeAuthorizationSession({
        issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
        client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
        browser: browser(callback), vault: vault(),
        key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
        createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
        createId: () => 'expected', exchange,
      })

      await expect(session.authorize()).rejects.toThrow('authorization_callback_invalid')
    }
    expect(exchange).not.toHaveBeenCalled()
  })

  it('rejects a pre-aborted authorization before registering a callback listener', async () => {
    const port = browser('termflow://auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111')
    const controller = new AbortController()
    controller.abort()
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: port, vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange: vi.fn(),
    })

    await expect(session.authorize(controller.signal)).rejects.toThrow('authorization_cancelled')
    expect(port.waitForCallback).not.toHaveBeenCalled()
    expect(port.open).not.toHaveBeenCalled()
  })

  it('cancels the callback listener when opening the system browser fails', async () => {
    let callbackSignal: AbortSignal | undefined
    const port: AuthorizationBrowserPort = {
      open: vi.fn().mockRejectedValue(new Error('browser unavailable')),
      waitForCallback: vi.fn((_state, signal) => new Promise<string>((_, reject) => {
        callbackSignal = signal
        signal?.addEventListener('abort', () => reject(new Error('authorization_cancelled')), { once: true })
      })),
    }
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: port, vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange: vi.fn(),
    })

    await expect(session.authorize()).rejects.toThrow('browser unavailable')
    expect(callbackSignal?.aborted).toBe(true)
  })

})

describe('parseNativeAuthorizationCallback', () => {
  it('parses a structurally valid app-scheme callback without an expected state', () => {
    expect(parseNativeAuthorizationCallback(
      'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
    )).toEqual({ state: 'state-1', transaction: '11111111-1111-4111-8111-111111111111' })
  })

  it('rejects anything that is not the strict two-parameter callback shape', () => {
    const malformed = [
      'https://attacker.example/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth:444/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://user@auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111#fragment',
      'termflow://auth/callback?state=state-1&transaction_id=not-a-uuid',
      'termflow://auth/callback?state=state-1',
      'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111&code=extra',
      'termflow://auth/callback?state=state-1&state=state-2&transaction_id=11111111-1111-4111-8111-111111111111',
      'not a url',
    ]
    for (const value of malformed) expect(parseNativeAuthorizationCallback(value)).toBeNull()
  })

  it('isValidNativeAuthorizationCallback still requires the expected state', () => {
    expect(isValidNativeAuthorizationCallback(
      'termflow://auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'expected',
    )).toBe(true)
    expect(isValidNativeAuthorizationCallback(
      'termflow://auth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'attacker',
    )).toBe(false)
  })
})

describe('parseLoopbackNativeAuthorizationCallback', () => {
  it('parses a structurally valid loopback callback without an expected state', () => {
    expect(parseLoopbackNativeAuthorizationCallback(
      'http://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
    )).toEqual({ state: 'state-1', transaction: '11111111-1111-4111-8111-111111111111' })
    expect(parseLoopbackNativeAuthorizationCallback(
      'http://[::1]:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
    )).toEqual({ state: 'state-1', transaction: '11111111-1111-4111-8111-111111111111' })
  })

  it('rejects anything outside the strict loopback callback shape', () => {
    const malformed = [
      'http://127.0.0.1:8765/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'http://127.0.0.1:70000/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'http://127.0.0.1:51234/other?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'http://127.0.0.1:51234/oauth/callback',
      'http://localhost:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'http://192.168.1.2:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'http://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111&code=extra',
      'http://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111#fragment',
      'http://user@127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'https://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
      'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
    ]
    for (const value of malformed) expect(parseLoopbackNativeAuthorizationCallback(value)).toBeNull()
  })

  it('isValidNativeAuthorizationCallback accepts the matching loopback handoff', () => {
    expect(isValidNativeAuthorizationCallback(
      'http://127.0.0.1:51234/oauth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'expected',
    )).toBe(true)
    expect(isValidNativeAuthorizationCallback(
      'http://127.0.0.1:51234/oauth/callback?state=expected&transaction_id=11111111-1111-4111-8111-111111111111',
      'attacker',
    )).toBe(false)
  })
})
