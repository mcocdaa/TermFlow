import { describe, expect, it, vi } from 'vitest'
import { createSecurityApi } from './security'

describe('security API', () => {
  it('uses explicit protection endpoints and returns configured status', async () => {
    const configured = { configured: true, enabled: false, available: true }
    const enabled = { configured: true, enabled: true, available: true }
    const request = vi.fn()
      .mockResolvedValueOnce(configured)
      .mockResolvedValueOnce(enabled)
      .mockResolvedValueOnce(configured)
    const security = createSecurityApi(request)

    await security.confirmTotpSetup('setup /1', '123456')
    await security.enableTotpProtection({ adminToken: 'admin', totpCode: '234567' })
    await security.disableTotpProtection({ adminToken: 'admin', totpCode: '345678' })

    expect(request.mock.calls).toEqual([
      ['/api/v1/admin/totp/setups/setup%20%2F1/confirm', { method: 'POST', body: { code: '123456' } }],
      ['/api/v1/admin/totp/enable', { method: 'POST', body: { admin_token: 'admin', code: '234567' } }],
      ['/api/v1/admin/totp', { method: 'DELETE', body: { admin_token: 'admin', code: '345678' } }],
    ])
  })
})
