import { describe, expect, it, vi } from 'vitest'
import type { OAuthTokenResponse } from '@termflow/client-contracts'
import type { CredentialVaultPort, NativeAccessCredential } from './ports'
import { DeviceAuthorizationSession } from './deviceAuthorization'

const token = {
  token_type: 'DPoP', access_token: 'access', expires_in: 60, refresh_token: 'refresh', scopes: ['terminal.read'],
} satisfies OAuthTokenResponse

const vault = (): CredentialVaultPort => ({
  load: vi.fn().mockResolvedValue(null), replace: vi.fn().mockResolvedValue(undefined), clear: vi.fn().mockResolvedValue(undefined),
})

const options = (poll: ReturnType<typeof vi.fn>, overrides: Record<string, unknown> = {}) => ({
  issuer: 'https://b.example', deviceCode: 'device', codeVerifier: 'verifier',
  publicJwk: { kty: 'EC' as const, crv: 'P-256' as const, alg: 'ES256' as const, x: 'x', y: 'y' },
  interval: 5, poll, vault: vault(), now: () => Date.parse('2026-08-04T00:00:00Z'),
  sleep: vi.fn().mockResolvedValue(undefined), ...overrides,
})

describe('DeviceAuthorizationSession', () => {
  it('waits for the server interval, retries pending, and stores only after success', async () => {
    const poll = vi.fn()
      // Tauri invoke rejects with the backend's string code, while HTTP
      // transports reject with ApiError; the core state machine accepts both.
      .mockRejectedValueOnce('authorization_pending')
      .mockResolvedValueOnce(token)
    const config = options(poll)
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).resolves.toMatchObject({ accessToken: 'access', tokenType: 'DPoP' } satisfies Partial<NativeAccessCredential>)
    expect(config.sleep).toHaveBeenNthCalledWith(1, 5000)
    expect(config.sleep).toHaveBeenNthCalledWith(2, 5000)
    expect(poll).toHaveBeenCalledTimes(2)
    expect(config.vault.replace).toHaveBeenCalledOnce()
  })
})
