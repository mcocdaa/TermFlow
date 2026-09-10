import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@termflow/client-core'
import { useDeviceAuthorizationApproval } from './useDeviceAuthorizationApproval'

const preview = {
  transaction_id: '11111111-1111-4111-8111-111111111111', issuer: 'https://relay.example',
  client_name: 'TermFlow Windows', platform: 'Windows', client_version: '0.1.0',
  key_fingerprint: 'fingerprint', scopes: ['terminal.read'], redirect_uri: 'termflow://auth/callback',
  totp_required: false, expires_at: '2026-08-05T12:00:00Z',
}

describe('useDeviceAuthorizationApproval', () => {
  it('normalizes a code and exposes the approved terminal state', async () => {
    const deviceAuthorizationPreview = vi.fn().mockResolvedValue(preview)
    const decideAuthorization = vi.fn().mockResolvedValue({ status: 'approved' })
    const approval = useDeviceAuthorizationApproval({ deviceAuthorizationPreview, decideAuthorization })

    await approval.lookup('abcd-efgh')
    await approval.decide('allow')

    expect(deviceAuthorizationPreview).toHaveBeenCalledWith('ABCD-EFGH')
    expect(decideAuthorization).toHaveBeenCalledWith({ transactionId: preview.transaction_id, decision: 'allow' })
    expect(approval.userCode.value).toBe('ABCD-EFGH')
    expect(approval.success.value).toBe('approved')
  })

  it('delegates an expired browser session without showing a device-code error', async () => {
    const onAuthenticationRequired = vi.fn().mockResolvedValue(undefined)
    const approval = useDeviceAuthorizationApproval(
      {
        deviceAuthorizationPreview: vi.fn().mockRejectedValue(new ApiError('authentication', { status: 401 })),
        decideAuthorization: vi.fn(),
      },
      { onAuthenticationRequired },
    )

    await approval.lookup('ABCD-EFGH')

    expect(onAuthenticationRequired).toHaveBeenCalledOnce()
    expect(approval.error.value).toBe('')
    expect(approval.preview.value).toBeNull()
  })
})
