import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import TotpProtectionDialog from './TotpProtectionDialog.vue'

describe('TotpProtectionDialog', () => {
  it('requires fresh credentials and emits the server-confirmed status', async () => {
    const enableTotpProtection = vi.fn().mockResolvedValue({ configured: true, enabled: true, available: true })
    const runtime = createFakeRuntime({
      api: { security: { enableTotpProtection } } as unknown as ClientRuntime['api'],
    })
    const wrapper = mount(TotpProtectionDialog, {
      props: { open: true, targetEnabled: true },
      global: { plugins: [createClientUi(runtime)] },
    })

    expect(wrapper.text()).not.toContain('请输入管理员 Token 和验证器刚生成的 6 位验证码。')
    expect(wrapper.get('label[for="protection-admin-token"]').text()).toBe('管理员 Token')
    expect(wrapper.get('label[for="protection-totp-code"]').text()).toBe('当前验证码')
    await wrapper.get('input[name="admin-token"]').setValue('admin-secret')
    await wrapper.get('input[name="totp-code"]').setValue('123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(enableTotpProtection).toHaveBeenCalledWith({ adminToken: 'admin-secret', totpCode: '123456' })
    expect(wrapper.emitted('confirmed')?.[0]).toEqual([{ configured: true, enabled: true, available: true }])
  })
})
