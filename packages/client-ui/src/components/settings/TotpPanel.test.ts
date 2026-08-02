import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import TotpPanel from './TotpPanel.vue'

async function mountPanel(runtime: ClientRuntime) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: { template: '<div />' } },
      { path: '/settings/two-factor-auth', component: { template: '<div />' } },
    ],
  })
  await router.push('/settings')
  await router.isReady()
  const wrapper = mount(TotpPanel, { global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  return { wrapper, router }
}

describe('TotpPanel', () => {
  it('offers Web onboarding without deployment-key terminology', async () => {
    const runtime = createFakeRuntime({
      api: { security: { totpStatus: vi.fn().mockResolvedValue({ configured: false, enabled: false, available: true }) } } as unknown as ClientRuntime['api'],
    })
    const { wrapper, router } = await mountPanel(runtime)

    expect(wrapper.get('.eyebrow').text()).toBe('Two Factor Authentication')
    expect(wrapper.get('#totp-heading').text()).toBe('双重因素认证')
    expect(wrapper.text()).not.toMatch(/主密钥|环境变量|文件路径/)
    await wrapper.get('[data-action="activate-totp"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings/two-factor-auth')
  })

  it('changes the switch only after a successful step-up', async () => {
    const enableTotpProtection = vi.fn().mockResolvedValue({ configured: true, enabled: true, available: true })
    const runtime = createFakeRuntime({
      api: {
        security: {
          totpStatus: vi.fn().mockResolvedValue({ configured: true, enabled: false, available: true }),
          enableTotpProtection,
        },
      } as unknown as ClientRuntime['api'],
    })
    const { wrapper } = await mountPanel(runtime)
    const toggle = wrapper.get('[role="switch"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
    await toggle.trigger('click')
    await wrapper.get('input[name="admin-token"]').setValue('admin-secret')
    await wrapper.get('input[name="totp-code"]').setValue('123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(toggle.attributes('aria-checked')).toBe('true')
  })

  it('keeps the switch unchanged and shows a generic error after rejection', async () => {
    const runtime = createFakeRuntime({
      api: {
        security: {
          totpStatus: vi.fn().mockResolvedValue({ configured: true, enabled: false, available: true }),
          enableTotpProtection: vi.fn().mockRejectedValue(new Error('internal detail')),
        },
      } as unknown as ClientRuntime['api'],
    })
    const { wrapper } = await mountPanel(runtime)
    const toggle = wrapper.get('[role="switch"]')
    await toggle.trigger('click')
    await wrapper.get('input[name="admin-token"]').setValue('admin-secret')
    await wrapper.get('input[name="totp-code"]').setValue('123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(toggle.attributes('aria-checked')).toBe('false')
    expect(wrapper.get('[role="alert"]').text()).toBe('验证失败，请检查凭据后重试。')
    expect(wrapper.text()).not.toContain('internal detail')
  })

  it('shows the generic unavailable state when status loading fails', async () => {
    const runtime = createFakeRuntime({
      api: {
        security: {
          totpStatus: vi.fn().mockRejectedValue(new Error('deployment detail')),
        },
      } as unknown as ClientRuntime['api'],
    })
    const { wrapper } = await mountPanel(runtime)

    expect(wrapper.get('[role="status"]').text()).toBe('双重因素认证暂时不可用，请联系服务器管理员。')
    expect(wrapper.text()).not.toContain('deployment detail')
  })
})
