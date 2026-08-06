import { describe, expect, it, vi } from 'vitest'
import type { OAuthTokenResponse } from '@termflow/client-contracts'
import { ApiError } from '../http/apiError'
import type { CredentialVaultPort, NativeAccessCredential } from './ports'
import type { AuthorizationState } from './authorizationState'
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

  it('adds five seconds after slow_down and stops on terminal errors', async () => {
    const poll = vi.fn().mockRejectedValueOnce(new ApiError('validation', { code: 'slow_down' })).mockRejectedValueOnce(new ApiError('validation', { code: 'access_denied' }))
    const config = options(poll)
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).rejects.toMatchObject({ code: 'access_denied' })
    expect(config.sleep).toHaveBeenNthCalledWith(1, 5000)
    expect(config.sleep).toHaveBeenNthCalledWith(2, 10000)
    expect(config.vault.replace).not.toHaveBeenCalled()
  })

  it('stops without polling when cancelled', async () => {
    const poll = vi.fn()
    const states: AuthorizationState[] = []
    const config = options(poll, { onState: (state: AuthorizationState) => states.push(state) })
    const session = new DeviceAuthorizationSession(config)
    const pending = session.authorize()
    session.cancel()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(poll).not.toHaveBeenCalled()
    expect(config.vault.replace).not.toHaveBeenCalled()
    expect(states).toEqual(['requesting', 'pending', 'cancelled'])
  })

  it('aborts an in-flight transport even when the injected transport ignores the signal', async () => {
    let resolvePoll!: (value: OAuthTokenResponse) => void
    const poll = vi.fn().mockImplementation(() => new Promise<OAuthTokenResponse>((resolve) => { resolvePoll = resolve }))
    const config = options(poll, { sleep: vi.fn().mockResolvedValue(undefined) })
    const session = new DeviceAuthorizationSession(config)
    const pending = session.authorize()
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    session.cancel()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(config.vault.replace).not.toHaveBeenCalled()
    resolvePoll?.(token)
  })

  it('accepts an access-only response when a native adapter keeps refresh material outside the WebView', async () => {
    const config = options(vi.fn().mockResolvedValue({
      accessToken: 'native-access', expiresAt: '2026-08-05T12:00:00Z', tokenType: 'DPoP',
    }))
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).resolves.toEqual({
      accessToken: 'native-access', expiresAt: '2026-08-05T12:00:00Z', tokenType: 'DPoP',
    })
    expect(config.vault.replace).toHaveBeenCalledWith('https://b.example', {
      accessToken: 'native-access', expiresAt: '2026-08-05T12:00:00Z', tokenType: 'DPoP',
    })
  })
})
