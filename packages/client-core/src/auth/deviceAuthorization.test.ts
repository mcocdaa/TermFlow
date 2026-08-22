import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../http/apiError'
import type { NativeAuthorizationStatus } from './ports'
import type { AuthorizationState } from './authorizationState'
import { DeviceAuthorizationSession } from './deviceAuthorization'

const status: NativeAuthorizationStatus = {
  authorized: true, expiresAt: '2026-08-21T12:00:00Z', tokenType: 'DPoP',
}

const options = (poll: ReturnType<typeof vi.fn>, overrides: Record<string, unknown> = {}) => ({
  issuer: 'https://b.example', deviceCode: 'device', codeVerifier: 'verifier',
  publicJwk: { kty: 'EC' as const, crv: 'P-256' as const, alg: 'ES256' as const, x: 'x', y: 'y' },
  interval: 5, poll, now: () => Date.parse('2026-08-04T00:00:00Z'),
  sleep: vi.fn().mockResolvedValue(undefined), ...overrides,
})

describe('DeviceAuthorizationSession', () => {
  it('waits for the server interval, retries pending, and returns the tokenless status', async () => {
    const poll = vi.fn()
      // Tauri invoke rejects with the backend's string code, while HTTP
      // transports reject with ApiError; the core state machine accepts both.
      .mockRejectedValueOnce('authorization_pending')
      .mockResolvedValueOnce(status)
    const config = options(poll)
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).resolves.toEqual(status)
    expect(config.sleep).toHaveBeenNthCalledWith(1, 5000)
    expect(config.sleep).toHaveBeenNthCalledWith(2, 5000)
    expect(poll).toHaveBeenCalledTimes(2)
  })

  it('adds five seconds after slow_down and stops on terminal errors', async () => {
    const poll = vi.fn().mockRejectedValueOnce(new ApiError('validation', { code: 'slow_down' })).mockRejectedValueOnce(new ApiError('validation', { code: 'access_denied' }))
    const config = options(poll)
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).rejects.toMatchObject({ code: 'access_denied' })
    expect(config.sleep).toHaveBeenNthCalledWith(1, 5000)
    expect(config.sleep).toHaveBeenNthCalledWith(2, 10000)
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
    expect(states).toEqual(['requesting', 'pending', 'cancelled'])
  })

  it('aborts an in-flight transport even when the injected transport ignores the signal', async () => {
    let resolvePoll!: (value: NativeAuthorizationStatus) => void
    const poll = vi.fn().mockImplementation(() => new Promise<NativeAuthorizationStatus>((resolve) => { resolvePoll = resolve }))
    const config = options(poll, { sleep: vi.fn().mockResolvedValue(undefined) })
    const session = new DeviceAuthorizationSession(config)
    const pending = session.authorize()
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    session.cancel()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    resolvePoll?.(status)
  })

  it('returns the tokenless status directly after a successful poll', async () => {
    const config = options(vi.fn().mockResolvedValue(status))
    const session = new DeviceAuthorizationSession(config)

    await expect(session.authorize()).resolves.toEqual(status)
    await expect(session.poll()).resolves.toEqual(status)
  })
})
