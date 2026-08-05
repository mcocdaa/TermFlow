import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invoke, tauriFetch, logNativeEvent } = vi.hoisted(() => ({
  invoke: vi.fn(),
  tauriFetch: vi.fn(),
  logNativeEvent: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))
vi.mock('@tauri-apps/plugin-http', () => ({ fetch: tauriFetch }))
vi.mock('../diagnostics', () => ({ logNativeEvent }))

import { serverConfig } from '../serverConfig'
import { createTauriHttpTransport } from './tauriHttpTransport'

describe('createTauriHttpTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    tauriFetch.mockReset()
    logNativeEvent.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('retries one resource request with the server DPoP nonce', async () => {
    const headers = [
      { authorization: 'DPoP access', dpop: 'proof-1' },
      { authorization: 'DPoP access', dpop: 'proof-2' },
    ]
    invoke.mockImplementation((command: string) => Promise.resolve(
      command === 'native_request_headers' ? headers.shift() : undefined,
    ))
    tauriFetch
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'use_dpop_nonce' } }), {
        status: 401,
        headers: { 'content-type': 'application/json', 'DPoP-Nonce': 'nonce-1' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.status).toBe(200)
    const headerCalls = invoke.mock.calls.filter(([command]) => command === 'native_request_headers')
    expect(headerCalls[1]).toEqual(['native_request_headers', expect.objectContaining({ nonce: 'nonce-1' })])
    expect(invoke).toHaveBeenCalledWith('native_remember_dpop_nonce', {
      issuer: 'https://b.example', nonce: 'nonce-1',
    })
    expect(tauriFetch).toHaveBeenCalledTimes(2)
    expect((tauriFetch.mock.calls[1]?.[1] as RequestInit).headers).toEqual(expect.objectContaining({}))
  })

  it('keeps device-code creation public before a credential exists', async () => {
    tauriFetch.mockResolvedValue(new Response(JSON.stringify({ device_code: 'd' }), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
    await createTauriHttpTransport().request('/api/v1/oauth/device/code', { method: 'POST', body: { client_name: 'TermFlow' } })
    expect(invoke).not.toHaveBeenCalledWith('native_request_headers', expect.anything())
  })

  it('does not retry a nonce challenge more than once', async () => {
    const headers = [
      { authorization: 'DPoP access', dpop: 'proof-1' },
      { authorization: 'DPoP access', dpop: 'proof-2' },
    ]
    invoke.mockImplementation((command: string) => Promise.resolve(
      command === 'native_request_headers' ? headers.shift() : undefined,
    ))
    tauriFetch.mockResolvedValue(new Response(undefined, {
      status: 401,
      headers: { 'DPoP-Nonce': 'nonce-1' },
    }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.status).toBe(401)
    expect(tauriFetch).toHaveBeenCalledTimes(2)
  })

  it('remembers the next nonce from a successful resource response for WebSockets', async () => {
    invoke.mockImplementation((command: string) => Promise.resolve(
      command === 'native_request_headers'
        ? { authorization: 'DPoP access', dpop: 'proof-1' }
        : undefined,
    ))
    tauriFetch.mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json', 'DPoP-Nonce': 'next-nonce' },
    }))

    await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(invoke).toHaveBeenCalledWith('native_remember_dpop_nonce', {
      issuer: 'https://b.example', nonce: 'next-nonce',
    })
  })

  it.each([
    new Error('error deserializing scope: `bad` is not a valid URL pattern'),
    'url not allowed on the configured scope: http://relay.example.com/',
  ])('classifies a Tauri HTTP scope failure separately from offline errors', async (failure) => {
    tauriFetch.mockRejectedValue(failure)

    await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'http_capability_denied' })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({
      event: 'http_request_failed', errorCode: 'http_capability_denied', level: 'error',
    }))
  })

  it('keeps ordinary fetch failures classified as offline', async () => {
    tauriFetch.mockRejectedValue(new TypeError('Failed to fetch https://relay.example/api?access_token=secret'))

    await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'offline' })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({
      event: 'http_request_failed', errorCode: 'offline', errorDetail: 'TypeError: Failed to fetch <url>',
    }))
  })
})
