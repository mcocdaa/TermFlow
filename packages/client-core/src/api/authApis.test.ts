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
    await api.oauth.deviceAuthorizationPreview('ABCD-EFGH')
    await api.oauth.decideAuthorization({ transactionId: 'tx', decision: 'allow' })
    await api.oauth.createDeviceAuthorization({
      clientName: 'TermFlow Desktop',
      platform: 'linux',
      clientVersion: '0.1.0',
      codeChallenge: 'challenge',
      dpopJkt: 'jkt',
      publicJwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' },
      scopes: ['terminal.read'],
    })
    await api.oauth.pollDeviceAuthorization({
      deviceCode: 'device-code',
      codeVerifier: 'verifier',
      publicJwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' },
    })
    await api.clients.update('client /1', 'Phone', ['terminal.read'], { adminToken: 'admin' })

    expect(request.mock.calls).toEqual([
      ['/api/v1/admin/sessions/challenge%20%2F1/totp', { method: 'POST', body: { code: '123456' } }],
      ['/api/v1/admin/totp/setups', { method: 'POST', body: { admin_token: 'admin', totp_code: '123456' } }],
      ['/api/v1/oauth/authorize?transaction_id=transaction%20%2F1', { method: 'GET' }],
      ['/api/v1/oauth/authorize?user_code=ABCD-EFGH', { method: 'GET' }],
      ['/api/v1/oauth/authorize', { method: 'POST', body: { transaction_id: 'tx', decision: 'allow' } }],
      ['/api/v1/oauth/device/code', { method: 'POST', body: {
        client_name: 'TermFlow Desktop',
        platform: 'linux',
        client_version: '0.1.0',
        code_challenge: 'challenge',
        code_challenge_method: 'S256',
        dpop_jkt: 'jkt',
        public_jwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' },
        scopes: ['terminal.read'],
      } }],
      ['/api/v1/oauth/token', { method: 'POST', body: {
        grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
        device_code: 'device-code',
        code_verifier: 'verifier',
        public_jwk: { kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' },
      } }],
      ['/api/v1/admin/clients/client%20%2F1', { method: 'PATCH', body: { display_name: 'Phone', scopes: ['terminal.read'], admin_token: 'admin' } }],
    ])
  })
})
