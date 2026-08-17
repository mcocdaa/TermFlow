import { describe, expect, it, vi } from 'vitest'
import { createApiClient } from './apiClient'
import type { HttpResponse } from './types'

const response = (status: number, body: unknown, contentType = 'application/json'): HttpResponse => ({
  status,
  headers: { get: (name) => name.toLowerCase() === 'content-type' ? contentType : null },
  body,
})

describe('createApiClient', () => {
  it('returns JSON and empty successful responses through a transport', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(response(200, { authenticated: true, expires_at: '2026-08-01T00:00:00Z' }))
      .mockResolvedValueOnce(response(204, undefined, ''))
    const api = createApiClient({ request })

    await expect(api.sessions.status()).resolves.toMatchObject({ authenticated: true })
    await expect(api.sessions.logout()).resolves.toBeUndefined()
    expect(request).toHaveBeenNthCalledWith(1, '/api/v1/admin/session', { method: 'GET' })
    expect(request).toHaveBeenNthCalledWith(2, '/api/v1/admin/session', { method: 'DELETE' })
  })
})
