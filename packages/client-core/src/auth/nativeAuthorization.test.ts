import { describe, expect, it, vi } from 'vitest'
import { NativeAuthorizationSession } from './nativeAuthorization'
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

    await expect(session.authorize(undefined, { forceLogin: true })).resolves.toMatchObject({ accessToken: 'access' })
    expect(new URL(vi.mocked(port.open).mock.calls[0]![0]).searchParams.get('prompt')).toBe('login')
    expect(port.open).toHaveBeenCalledOnce()
    expect(vi.mocked(port.waitForCallback).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(port.open).mock.invocationCallOrder[0]!)
    expect(exchange).toHaveBeenCalledWith(expect.objectContaining({ transaction: '11111111-1111-4111-8111-111111111111', verifier: 'v'.repeat(43) }))
    expect(store.replace).toHaveBeenCalledWith('https://b.example', expect.objectContaining({ accessToken: 'access' }))
    expect(states).toEqual(['requesting', 'pending', 'approved', 'connected'])
  })
})
