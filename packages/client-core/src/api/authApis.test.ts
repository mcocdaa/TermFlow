import { describe, expect, it, vi } from 'vitest'
import type { HttpResponse } from '../http/types'
import { createApiClient } from '../http/apiClient'

const response = (status: number, body: unknown): HttpResponse => ({ status, body, headers: { get: () => null } })

describe('authentication APIs', () => {
  it('models the Web login challenge without retaining the administrator token', async () => {
    const request = vi.fn().mockResolvedValue(response(202, { status: 'totp_required', challenge_id: 'challenge', expires_at: '2026-08-02T12:00:00Z' }))
    const result = await createApiClient({ request }).sessions.login('bootstrap-secret')

    expect(result).toMatchObject({ status: 'totp_required' })
    expect(JSON.stringify(result)).not.toContain('bootstrap-secret')
  })

  it('uses fixed security, OAuth, and client-management paths', async () => {
    const request = vi.fn().mockResolvedValue(response(200, {}))
    const api = createApiClient({ request })
    await api.sessions.completeTotp('challenge /1', '123456')
    await api.security.createTotpSetup({ adminToken: 'admin', totpCode: '123456' })
    await api.oauth.authorizationPreview('transaction /1')
    await api.clients.update('client /1', 'Phone', ['terminal.read'], { adminToken: 'admin' })

    expect(request.mock.calls).toEqual([
      ['/api/v1/admin/sessions/challenge%20%2F1/totp', { method: 'POST', body: { code: '123456' } }],
      ['/api/v1/admin/totp/setups', { method: 'POST', body: { admin_token: 'admin', totp_code: '123456' } }],
      ['/api/v1/oauth/authorize?transaction_id=transaction%20%2F1', { method: 'GET' }],
      ['/api/v1/admin/clients/client%20%2F1', { method: 'PATCH', body: { display_name: 'Phone', scopes: ['terminal.read'], admin_token: 'admin' } }],
    ])
  })
})
