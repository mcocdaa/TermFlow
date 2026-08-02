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
    const callback = 'termflow://auth/callback?state=state-1&transaction=tx-public&issuer=https%3A%2F%2Fb.example'
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
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'state-1',
      exchange,
    })

    await expect(session.authorize()).resolves.toMatchObject({ accessToken: 'access' })
    expect(port.open).toHaveBeenCalledOnce()
    expect(exchange).toHaveBeenCalledWith(expect.objectContaining({ transaction: 'tx-public', verifier: 'v'.repeat(43) }))
    expect(store.replace).toHaveBeenCalledWith('https://b.example', expect.objectContaining({ accessToken: 'access' }))
  })

  it('rejects callbacks with the wrong state before token exchange', async () => {
    const exchange = vi.fn()
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: browser('termflow://auth/callback?state=attacker&transaction=tx&issuer=https%3A%2F%2Fb.example'), vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange,
    })

    await expect(session.authorize()).rejects.toThrow('authorization_callback_invalid')
    expect(exchange).not.toHaveBeenCalled()
  })

  it('rejects an issuer mix-up callback', async () => {
    const exchange = vi.fn()
    const session = new NativeAuthorizationSession({
      issuer: 'https://b.example', authorizeEndpoint: 'https://b.example/api/v1/oauth/authorize',
      client: { name: 'Desktop', platform: 'linux', version: '1' }, scopes: ['terminal.read'],
      browser: browser('termflow://auth/callback?state=expected&transaction=tx&issuer=https%3A%2F%2Fevil.example'), vault: vault(),
      key: { publicJwk: async () => ({ kty: 'EC', crv: 'P-256', x: 'x', y: 'y' }), thumbprint: async () => 'jkt', signJwt: async () => new Uint8Array() },
      createPkce: async () => ({ verifier: 'v'.repeat(43), challenge: 'c'.repeat(43), method: 'S256' }),
      createId: () => 'expected', exchange,
    })

    await expect(session.authorize()).rejects.toThrow('authorization_callback_invalid')
    expect(exchange).not.toHaveBeenCalled()
  })
})
