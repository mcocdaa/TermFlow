import { describe, expect, it, vi } from 'vitest'
import { NativeAuthorizationSession } from './nativeAuthorization'
import type { AuthorizationBrowserPort, CredentialVaultPort, NativeAccessCredential } from './ports'

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
    })

    await expect(session.authorize()).resolves.toMatchObject({ accessToken: 'access' })
    expect(port.open).toHaveBeenCalledOnce()
    expect(vi.mocked(port.waitForCallback).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(port.open).mock.invocationCallOrder[0]!)
    expect(exchange).toHaveBeenCalledWith(expect.objectContaining({ transaction: '11111111-1111-4111-8111-111111111111', verifier: 'v'.repeat(43) }))
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

})
