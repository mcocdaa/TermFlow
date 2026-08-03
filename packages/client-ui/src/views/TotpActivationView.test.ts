import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import TotpActivationView from './TotpActivationView.vue'

vi.mock('qrcode', () => ({
  default: { toString: vi.fn().mockResolvedValue('<svg><path /></svg>') },
}))

describe('TotpActivationView', () => {
  it('guides setup, leaves protection off, then enables with a fresh code', async () => {
    const createTotpSetup = vi.fn().mockResolvedValue({
      setup_id: 'setup-1',
      provisioning_uri: 'otpauth://totp/TermFlow?secret=SETUPKEY&issuer=TermFlow&algorithm=SHA1&digits=6&period=30',
      setup_key: 'SETUPKEY',
      expires_at: '2026-08-02T12:10:00Z',
    })
    const confirmTotpSetup = vi.fn().mockResolvedValue({ configured: true, enabled: false, available: true })
    const enableTotpProtection = vi.fn().mockResolvedValue({ configured: true, enabled: true, available: true })
    const runtime = createFakeRuntime({
      api: {
        security: {
          totpStatus: vi.fn().mockResolvedValue({ configured: false, enabled: false, available: true }),
          createTotpSetup,
          confirmTotpSetup,
          enableTotpProtection,
        },
      } as unknown as ClientRuntime['api'],
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/settings', component: { template: '<div />' } },
      { path: '/settings/two-factor-auth', component: TotpActivationView },
    ] })
    await router.push('/settings/two-factor-auth')
    await router.isReady()
    const wrapper = mount(TotpActivationView, { global: { plugins: [router, createClientUi(runtime)] } })
    await flushPromises()

    expect(wrapper.findAll('[data-guide-step]')).toHaveLength(5)
    expect(wrapper.text()).not.toContain('管理员 Token 只用于本次验证，不会保存在客户端。')
    expect(wrapper.text()).not.toContain('使用你的验证器 App 完成绑定')
    await wrapper.get('input[name="setup-admin-token"]').setValue('admin-secret')
    await wrapper.get('[data-action="begin-totp-setup"]').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('SETUPKEY')
    expect(wrapper.find('.themed-qr-code').exists()).toBe(true)
    expect(wrapper.get('[data-setup-key-label]').text()).toBe('设置密钥')
    expect(wrapper.get('[data-action="explain-setup-key"]').attributes('aria-label')).toBe('说明设置密钥')
    expect(wrapper.get('[data-context-help] [role="tooltip"]').text()).toContain('无法扫码')
    expect(wrapper.text()).not.toContain('在验证器 App 中扫码，或手工输入下面的设置密钥。')

    await wrapper.get('input[name="setup-confirm-code"]').setValue('123456')
    await wrapper.get('[data-action="confirm-totp-setup"]').trigger('submit')
    await flushPromises()
    expect(confirmTotpSetup).toHaveBeenCalledWith('setup-1', '123456')
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    expect(wrapper.get('[data-totp-protection-label]').classes()).toContain('security-setting-label')
    expect(wrapper.get('[data-action="explain-totp-protection"]').attributes('aria-label')).toBe('说明启用双重认证登录')
    const configuredHeading = wrapper.get('[data-configured-authenticator-heading]')
    expect(configuredHeading.get('h2').text()).toBe('验证器已绑定')
    expect(configuredHeading.get('[data-action="reconfigure-totp"]').text()).toBe('重新配置')
    expect(wrapper.find('.totp-guide-card > .settings-action-button').exists()).toBe(false)

    await wrapper.get('[role="switch"]').trigger('click')
    await wrapper.get('input[name="admin-token"]').setValue('admin-again')
    await wrapper.get('input[name="totp-code"]').setValue('234567')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('true')
  })

  it('shows the generic unavailable state when status loading fails', async () => {
    const runtime = createFakeRuntime({
      api: {
        security: {
          totpStatus: vi.fn().mockRejectedValue(new Error('deployment detail')),
        },
      } as unknown as ClientRuntime['api'],
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/settings', component: { template: '<div />' } },
      { path: '/settings/two-factor-auth', component: TotpActivationView },
    ] })
    await router.push('/settings/two-factor-auth')
    await router.isReady()
    const wrapper = mount(TotpActivationView, { global: { plugins: [router, createClientUi(runtime)] } })
    await flushPromises()

    expect(wrapper.get('[role="status"]').text()).toBe('双重因素认证暂时不可用，请联系服务器管理员。')
    expect(wrapper.text()).not.toContain('deployment detail')
  })
})
