import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './apiError'
import { createApiClient } from './apiClient'
import { HttpTransportError, type HttpResponse, type HttpTransport } from './types'

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

  it.each([
    [401, 'authentication', '会话已过期，请重新登录。'],
    [422, 'validation', '提交的内容不符合要求。'],
    [429, 'rate_limit', '操作过于频繁，请稍后重试。'],
    [503, 'server', '服务暂时不可用，请稍后重试。'],
  ] as const)('maps status %i to a safe structured error', async (status, kind, message) => {
    const transport: HttpTransport = {
      request: vi.fn().mockResolvedValue(response(status, {
        error: { code: 'safe_code', message: 'raw server stack', request_id: 'request-1' },
      })),
    }
    const error = await createApiClient(transport).dashboard.get().catch((caught) => caught) as ApiError

    expect(error).toMatchObject({ kind, status, code: 'safe_code', requestId: 'request-1' })
    expect(error.message).toBe(message)
    expect(error.message).not.toContain('stack')
  })

  it('does not expose malformed error bodies', async () => {
    const api = createApiClient({ request: vi.fn().mockResolvedValue(response(500, 'secret trace')) })
    const error = await api.dashboard.get().catch((caught) => caught) as ApiError
    expect(error).toMatchObject({ kind: 'server', status: 500 })
    expect(error.code).toBeUndefined()
    expect(error.message).not.toContain('secret')
  })

  it.each([
    ['aborted', 'aborted'],
    ['offline', 'offline'],
  ] as const)('maps the %s transport failure without logging payloads', async (transportKind, apiKind) => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const api = createApiClient({ request: vi.fn().mockRejectedValue(new HttpTransportError(transportKind)) })
    await expect(api.dashboard.get()).rejects.toMatchObject({ kind: apiKind })
    expect(log).not.toHaveBeenCalled()
  })

  it('uses fixed public paths and request bodies for every current use case', async () => {
    const request = vi.fn().mockResolvedValue(response(200, {}))
    const api = createApiClient({ request })

    await api.sessions.login('admin-secret')
    await api.dashboard.get()
    await api.computers.list()
    await api.computers.get('computer /1')
    await api.computers.rename('computer-1', 'Studio')
    await api.computers.createEnrollment('Studio')
    await api.terms.topology('term /1')
    await api.terms.rename('term-1', 'Editor')

    expect(request.mock.calls).toEqual([
      ['/api/v1/admin/sessions', { method: 'POST', body: { admin_token: 'admin-secret' } }],
      ['/api/v1/dashboard', { method: 'GET' }],
      ['/api/v1/computers', { method: 'GET' }],
      ['/api/v1/computers/computer%20%2F1', { method: 'GET' }],
      ['/api/v1/computers/computer-1', { method: 'PATCH', body: { display_name: 'Studio' } }],
      ['/api/v1/enrollment-tokens', { method: 'POST', body: { display_name: 'Studio' } }],
      ['/api/v1/instances/term%20%2F1/topology', { method: 'GET' }],
      ['/api/v1/terms/term-1', { method: 'PATCH', body: { name: 'Editor' } }],
    ])
  })
})
