import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import SettingsView from './SettingsView.vue'

async function mountSettings(runtime: ClientRuntime) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsView }] })
  await router.push('/settings'); await router.isReady()
  const wrapper = mount(SettingsView, { global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  return wrapper
}

describe('SettingsView', () => {
  it('uses concise Settings copy and the metadata issuer', async () => {
    const runtime = createFakeRuntime({
      canonicalServerUrl: 'https://browser-origin.invalid',
      api: {
        oauth: { metadata: vi.fn().mockResolvedValue({ issuer: 'https://configured-b.example' }) },
        security: { totpStatus: vi.fn().mockResolvedValue({ configured: false, enabled: false, available: true }) },
        clients: { list: vi.fn().mockResolvedValue({ clients: [] }) },
      } as unknown as ClientRuntime['api'],
    })
    const wrapper = await mountSettings(runtime)

    expect(wrapper.get('.page-heading .eyebrow').text()).toBe('Settings')
    expect(wrapper.get('.page-heading h1').text()).toBe('设置')
    expect(wrapper.text()).not.toContain('主题在客户端本地保存')
    expect(wrapper.text()).not.toContain('Preferences & Security')
    expect(wrapper.get('[data-server-issuer]').text()).toBe('https://configured-b.example')
    expect(wrapper.find('input[value="https://configured-b.example"]').exists()).toBe(false)
  })

  it('hides Web-only security management for native capabilities', async () => {
    const runtime = createFakeRuntime({
      capabilities: { manageSecurity: false, manageAuthorizedClients: false },
      api: { oauth: { metadata: vi.fn().mockResolvedValue({ issuer: 'https://b.example' }) } } as unknown as ClientRuntime['api'],
    })
    const wrapper = await mountSettings(runtime)

    expect(wrapper.text()).toContain('安全设置只能从已认证的网页管理端修改')
    expect(wrapper.find('#totp-admin-token').exists()).toBe(false)
  })
})
