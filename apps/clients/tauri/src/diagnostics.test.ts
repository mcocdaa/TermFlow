import { invoke } from '@tauri-apps/api/core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { logNativeEvent, sanitizeNativeDetail } from './diagnostics'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))

describe('native diagnostics', () => {
  beforeEach(() => vi.mocked(invoke).mockReset())

  it('sends only the structured safe event fields', async () => {
    await logNativeEvent({ event: 'metadata_success', issuer: 'https://relay.example', requestId: 'req-1' })
    expect(invoke).toHaveBeenCalledWith('native_log', {
      event: 'metadata_success',
      level: undefined,
      issuer: 'https://relay.example',
      requestId: 'req-1',
      errorCode: undefined,
      errorDetail: undefined,
    })
  })

  it('does not throw when native logging is unavailable', async () => {
    vi.mocked(invoke).mockRejectedValueOnce(new Error('no native runtime'))
    await expect(logNativeEvent({ event: 'connect_started' })).resolves.toBeUndefined()
  })

  it('forwards a bounded diagnostic detail without changing the event shape', async () => {
    await logNativeEvent({ event: 'browser_open_failed', errorCode: 'browser_open_failed', errorDetail: 'Error: shell execute failed' })
    expect(invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
      errorCode: 'browser_open_failed',
      errorDetail: 'Error: shell execute failed',
    }))
  })

  it('redacts credential-shaped body, authorization, and DPoP details', () => {
    const detail = sanitizeNativeDetail(new Error('request body {"access_token":"secret","refresh_token":"refresh"} Authorization: Bearer bearer-secret DPoP: eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiIxIn0.signature'))
    expect(detail).not.toContain('secret')
    expect(detail).not.toContain('bearer-secret')
    expect(detail).not.toContain('eyJ')
    expect(detail).not.toContain('"refresh_token":"refresh"')
    expect(detail).toContain('<redacted>')
    expect(detail.length).toBeLessThanOrEqual(256)
  })

  it('redacts device authorization codes', () => {
    const detail = sanitizeNativeDetail(new Error(
      'device_code=secret-device user_code=ABCD-EFGH code_verifier=verifier',
    ))

    expect(detail).not.toContain('secret-device')
    expect(detail).not.toContain('ABCD-EFGH')
    expect(detail).not.toContain('code_verifier=verifier')
    expect(detail).toContain('code_verifier=<redacted>')
  })
})
