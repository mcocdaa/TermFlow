import { describe, expect, it, vi } from 'vitest'
import { createSession, deleteSession, getSessionStatus } from './session'

describe('admin session API contract', () => {
  it('uses the approved plural create route and singular status/delete route', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ authenticated: true }), { status: 201, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ authenticated: true }), { status: 200, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await createSession('secret')
    await getSessionStatus()
    await deleteSession()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/v1/admin/sessions', expect.objectContaining({ method: 'POST', body: JSON.stringify({ admin_token: 'secret' }) }))
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/admin/session')
    expect(fetchMock.mock.calls[1]?.[1]).not.toHaveProperty('method')
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/v1/admin/session', expect.objectContaining({ method: 'DELETE' }))
  })
})
