import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import DeviceAuthorizationApprovalDialog from './DeviceAuthorizationApprovalDialog.vue'

const preview = {
  transaction_id: '11111111-1111-4111-8111-111111111111', issuer: 'https://relay.example',
  client_name: 'TermFlow Windows', platform: 'Windows', client_version: '0.1.0',
  key_fingerprint: 'fingerprint', scopes: ['terminal.read'], redirect_uri: 'termflow://auth/callback',
  totp_required: false, expires_at: '2026-08-05T12:00:00Z',
}

describe('DeviceAuthorizationApprovalDialog', () => {
  it('looks up and approves a device code from settings', async () => {
    const deviceAuthorizationPreview = vi.fn().mockResolvedValue(preview)
    const decideAuthorization = vi.fn().mockResolvedValue({ status: 'approved' })
    const runtime = createFakeRuntime({ api: { oauth: { deviceAuthorizationPreview, decideAuthorization } } as unknown as ClientRuntime['api'] })
    const wrapper = mount(DeviceAuthorizationApprovalDialog, { global: { plugins: [createClientUi(runtime)] } })

    await wrapper.get('#device-approval-code').setValue('abcd-efgh')
    await wrapper.get('[data-action="lookup-device-approval"]').trigger('submit')
    await flushPromises()

    expect(deviceAuthorizationPreview).toHaveBeenCalledWith('ABCD-EFGH')
    expect(wrapper.text()).toContain('TermFlow Windows')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(decideAuthorization).toHaveBeenCalledWith({ transactionId: preview.transaction_id, decision: 'allow' })
    expect(wrapper.emitted('approved')).toHaveLength(1)
  })
})
