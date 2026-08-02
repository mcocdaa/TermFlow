import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invoke, tauriFetch } = vi.hoisted(() => ({
  invoke: vi.fn(),
  tauriFetch: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))
vi.mock('@tauri-apps/plugin-http', () => ({ fetch: tauriFetch }))

import { serverConfig } from '../serverConfig'
import { createTauriHttpTransport } from './tauriHttpTransport'

describe('createTauriHttpTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    tauriFetch.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('retries one resource request with the server DPoP nonce', async () => {
    invoke
      .mockResolvedValueOnce({ authorization: 'DPoP access', dpop: 'proof-1' })
      .mockResolvedValueOnce({ authorization: 'DPoP access', dpop: 'proof-2' })
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
    expect(invoke).toHaveBeenNthCalledWith(2, 'native_request_headers', expect.objectContaining({ nonce: 'nonce-1' }))
    expect(tauriFetch).toHaveBeenCalledTimes(2)
    expect((tauriFetch.mock.calls[1]?.[1] as RequestInit).headers).toEqual(expect.objectContaining({}))
  })

  it('does not retry a nonce challenge more than once', async () => {
    invoke
      .mockResolvedValueOnce({ authorization: 'DPoP access', dpop: 'proof-1' })
      .mockResolvedValueOnce({ authorization: 'DPoP access', dpop: 'proof-2' })
    tauriFetch.mockResolvedValue(new Response(undefined, {
      status: 401,
      headers: { 'DPoP-Nonce': 'nonce-1' },
    }))

    const response = await createTauriHttpTransport().request('/api/v1/dashboard', { method: 'GET' })

    expect(response.status).toBe(401)
    expect(tauriFetch).toHaveBeenCalledTimes(2)
  })
})
