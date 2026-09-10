import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invoke, logNativeEvent } = vi.hoisted(() => ({
  invoke: vi.fn(),
  logNativeEvent: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))
vi.mock('../diagnostics', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../diagnostics')>()
  return { ...actual, logNativeEvent }
})

import { serverConfig } from '../serverConfig'
import { createTauriHttpTransport } from './tauriHttpTransport'

function nativeResponse(overrides: Partial<{ status: number; headers: Record<string, string>; body: unknown }> = {}) {
  return {
    status: overrides.status ?? 200,
    headers: overrides.headers ?? { 'content-type': 'application/json' },
    body: overrides.body ?? undefined,
  }
}

describe('createTauriHttpTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    logNativeEvent.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('retries one resource request with the server DPoP nonce', async () => {
    invoke
      .mockResolvedValueOnce(nativeResponse({ status: 401, headers: { 'dpop-nonce': 'nonce-1' } }))
      .mockResolvedValueOnce(nativeResponse({ body: { ok: true } }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.status).toBe(200)
    expect(response.body).toEqual({ ok: true })
    const calls = invoke.mock.calls.filter(([command]) => command === 'native_http_request')
    expect(calls).toHaveLength(2)
    expect(calls[1]).toEqual(['native_http_request', expect.objectContaining({ nonce: 'nonce-1' })])
  })

  it('keeps device-code creation public before a credential exists', async () => {
    invoke.mockResolvedValue(nativeResponse({ body: { device_code: 'd' } }))
    await createTauriHttpTransport().request('/api/v1/oauth/device/code', { method: 'POST', body: { client_name: 'TermFlow' } })
    expect(invoke).toHaveBeenCalledWith('native_http_request', expect.objectContaining({ path: '/api/v1/oauth/device/code', body: { client_name: 'TermFlow' } }))
  })

  it('does not retry a nonce challenge more than once', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 401, headers: { 'dpop-nonce': 'nonce-1' } }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.status).toBe(401)
    expect(invoke.mock.calls.filter(([command]) => command === 'native_http_request')).toHaveLength(2)
  })

  it('exposes response headers through a case-insensitive reader', async () => {
    invoke.mockResolvedValue(nativeResponse({ headers: { 'x-request-id': 'req-1' } }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.headers.get('X-Request-ID')).toBe('req-1')
  })

  it.each([
    'url_not_allowed',
    'method_not_allowed',
  ])('classifies a native URL denial separately from offline errors', async (errorCode) => {
    invoke.mockRejectedValue(errorCode)

    await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'http_capability_denied' })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({
      event: 'http_request_failed', errorCode: 'http_capability_denied', level: 'error',
    }))
  })

  it('keeps ordinary native failures classified as offline', async () => {
    invoke.mockRejectedValue('request_failed')

    await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'offline' })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({
      event: 'http_request_failed', errorCode: 'offline',
    }))
  })
})
